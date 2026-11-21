"""Transactional, content-addressed publication storage.

Layout below ``data_dir``::

    state.sqlite3              publication journal (source of truth for GC)
    incoming/<token>/          uploaded files being validated
    export/                    the HTTP document root (never written in place)
        dists/<suite>/Release
        dists/<suite>/<comp>/binary-<arch>/Packages.gz
        dists/<suite>/<comp>/binary-<arch>/by-hash/SHA256/<sha256>
        dists/<suite>/by-hash/SHA256/<sha256>          (Release by-hash)
        pool/<comp>/<bucket>/<pkg>/<pkg>_<ver>_<arch>_<sha16>.deb

Publication protocol
--------------------
1. Validate and hash every uploaded package (no repository state is touched
   if any package is invalid).
2. Record a ``staged`` journal row containing every file the publication owns.
3. Write immutable pool and by-hash files, fsync them, then write the fixed
   ``Packages.gz`` path (atomic rename).
4. Atomically rename ``Release`` into place - this is the commit point.
5. Mark the journal row ``committed``; garbage-collect everything that is not
   referenced by the newest ``retention`` committed publications.

Crash recovery
--------------
A row left ``staged`` is resolved by comparing the live ``Release`` hash with
the hash recorded in the row: equal means the final rename happened and the
publication is committed, otherwise it is aborted and its files are collected.
Because the previous committed publications are never modified, the last two
releases remain fully usable after any interruption.

Data validation cache
---------------------
The ``data_checksums`` table maps ``(data member compression, raw SHA-256 of
the data.tar member)`` to the publication that validated those bytes by
decompressing and walking the whole archive.  Rows are written while the
publication is ``staged`` but only become visible through
:meth:`Repository.find_validated` once the publication is ``committed`` (also
after crash recovery), so interrupted publications never grant reuse.  The
table is not touched by garbage collection: validation records stay reusable
after the originating pool file was reclaimed and across service restarts.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import Settings
from .deb import DebError, DebInfo, inspect_deb
from .indexer import (
    PackageEntry,
    gzip_bytes,
    pool_filename,
    render_packages,
    render_release,
    sha256_hex,
)

_CHUNK = 1024 * 1024


class PublicationError(Exception):
    """A publication could not be completed; repository state is unchanged."""


@dataclass(frozen=True)
class StagedUpload:
    path: Path
    original_name: str


@dataclass(frozen=True)
class PreparedPublication:
    """An immutable view of a batch that is ready to be committed."""

    pairs: tuple[tuple[DebInfo, PackageEntry], ...]
    index: bytes
    index_gz: bytes
    release: bytes
    index_sha256: str
    index_size: int
    plain_index_sha256: str
    plain_index_size: int
    uploads: dict[str, StagedUpload]
    #: ``(package, version)`` pairs whose data archive validation was reused
    #: from an earlier committed publication.
    reused: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Publication:
    id: int
    created_at: str
    state: str
    release_sha256: str
    index_sha256: str
    index_size: int
    package_count: int
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "state": self.state,
            "release_sha256": self.release_sha256,
            "index_sha256": self.index_sha256,
            "index_size": self.index_size,
            "package_count": self.package_count,
            "error": self.error,
        }


@dataclass(frozen=True)
class FileLease:
    """An open descriptor for a published file.

    Serving through an open descriptor makes downloads immune to garbage
    collection: POSIX keeps the inode alive even if the name is unlinked while
    the response is streaming. The descriptor is closed by the response's
    background task.
    """

    fh: object
    size: int
    relpath: str
    last_modified: float
    etag: str

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.fh.close()  # type: ignore[attr-defined]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(target: Path, data: bytes) -> None:
    """Write *data* to *target* atomically and durably."""

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("xb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    _fsync_dir(target.parent)


def _atomic_move(source: Path, target: Path) -> None:
    """Durably move an already-fsynced file into its final location."""

    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    _fsync_dir(target.parent)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.data_dir
        self.export = self.root / "export"
        self.incoming = self.root / "incoming"
        self.db_path = self.root / "state.sqlite3"
        self.lock_path = self.root / ".publish.lock"
        self._thread_lock = threading.Lock()
        self._initialise()

    # ------------------------------------------------------------------ setup
    @property
    def release_abspath(self) -> Path:
        return self.export / self.settings.release_path

    def _initialise(self) -> None:
        for directory in (self.root, self.export, self.incoming):
            directory.mkdir(parents=True, exist_ok=True)
        self._ensure_index_dirs()
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS publications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('staged','committed','aborted')),
                    release_sha256 TEXT NOT NULL,
                    index_sha256 TEXT NOT NULL,
                    plain_index_sha256 TEXT NOT NULL,
                    index_size INTEGER NOT NULL,
                    package_count INTEGER NOT NULL,
                    packages_json TEXT NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS publication_files (
                    publication_id INTEGER NOT NULL REFERENCES publications(id),
                    path TEXT NOT NULL,
                    role TEXT NOT NULL,
                    PRIMARY KEY (publication_id, path)
                );
                CREATE TABLE IF NOT EXISTS data_checksums (
                    compression TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    publication_id INTEGER NOT NULL REFERENCES publications(id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (compression, sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_data_checksums_publication
                    ON data_checksums(publication_id);
                """
            )
        self.reconcile()

    def _ensure_index_dirs(self) -> None:
        s = self.settings
        for relative in (
            f"dists/{s.distribution}/{s.index_dir}/by-hash/SHA256",
            f"dists/{s.distribution}/by-hash/SHA256",
        ):
            (self.export / relative).mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------- database
    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Serialise publications across threads *and* processes."""

        with self._thread_lock:
            with self.lock_path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # ---------------------------------------------------------------- queries
    def _row_to_publication(self, row: sqlite3.Row) -> Publication:
        return Publication(
            id=row["id"],
            created_at=row["created_at"],
            state=row["state"],
            release_sha256=row["release_sha256"],
            index_sha256=row["index_sha256"],
            index_size=row["index_size"],
            package_count=row["package_count"],
            error=row["error"],
        )

    def list_publications(self, limit: int = 20) -> list[Publication]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM publications ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_publication(row) for row in rows]

    def current_publication(self) -> Publication | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM publications WHERE state='committed' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._row_to_publication(row) if row else None

    # ------------------------------------------------- data validation cache
    def find_validated(self, compression: str, digest: str) -> bool:
        """Return True when a *committed* publication validated this exact
        ``(compression, raw data archive sha256)`` pair.

        Records owned by ``staged`` or ``aborted`` publications never match:
        an interrupted publication must not leave reusable validation
        results, and the join also makes the rows survive garbage collection
        of the packages themselves and full service restarts.
        """

        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM data_checksums c "
                "JOIN publications p ON p.id = c.publication_id "
                "WHERE c.compression = ? AND c.sha256 = ? AND p.state = 'committed' "
                "LIMIT 1",
                (compression, digest),
            ).fetchone()
        return row is not None

    def _remember_validated(
        self, db: sqlite3.Connection, publication_id: int, created_at: str,
        pairs: tuple[tuple[DebInfo, PackageEntry], ...],
    ) -> None:
        """Record every freshly validated data archive of this publication.

        Rows are written while the publication is still ``staged`` and only
        become visible through :meth:`find_validated` once the publication
        commits.  If an earlier, since-aborted publication already stored the
        same key (the commit never happened), the row is rebound to this
        publication; rows owned by a committed publication are immutable.
        """

        rows = []
        seen: set[tuple[str, str]] = set()
        for info, _entry in pairs:
            if info.data_archive is None or info.data_validation_reused:
                continue
            key = (info.data_archive.compression, info.data_archive.sha256)
            if key in seen:
                continue
            seen.add(key)
            rows.append((key[0], key[1], publication_id, created_at))
        if not rows:
            return
        # Rows owned by committed publications are immutable, so an existing
        # key is simply ignored.  A key owned by a non-committed publication
        # comes from an interrupted earlier attempt: rebound it here, and it
        # becomes reusable as soon as *this* publication commits.
        db.executemany(
            "INSERT OR IGNORE INTO data_checksums (compression, sha256, "
            "publication_id, created_at) VALUES (?, ?, ?, ?)",
            rows,
        )
        db.executemany(
            "UPDATE data_checksums SET publication_id = ?, created_at = ? "
            "WHERE compression = ? AND sha256 = ? AND publication_id IN "
            "(SELECT id FROM publications WHERE state != 'committed')",
            [
                (publication_id, created_at, compression, digest)
                for compression, digest, publication_id, created_at in rows
            ],
        )

    # ------------------------------------------------------------- publishing
    def publish(
        self, uploads: list[StagedUpload]
    ) -> tuple[Publication, tuple[tuple[str, str], ...]]:
        """Validate and publish *uploads*.

        Returns the :class:`Publication` together with the
        ``(package, version)`` pairs whose data archive integrity check was
        reused from a previously committed publication.
        """

        if not uploads:
            raise PublicationError("a publication must contain at least one package")

        prepared = self._prepare(uploads)
        with self._exclusive():
            publication = self._commit(prepared)
        return publication, prepared.reused

    def _prepare(self, uploads: list[StagedUpload]) -> PreparedPublication:
        """Validate/hash the batch and build the new index and Release bytes."""

        pairs: list[tuple[DebInfo, PackageEntry]] = []
        seen_identity: dict[tuple[str, str, str], str] = {}
        seen_path: dict[str, str] = {}
        uploads_by_dest: dict[str, StagedUpload] = {}
        reused: list[tuple[str, str]] = []

        for upload in uploads:
            try:
                info = inspect_deb(
                    upload.path, self.settings.architecture, cache=self
                )
            except DebError as exc:
                raise PublicationError(
                    f"{upload.original_name}: {exc}"
                ) from exc
            if info.data_validation_reused:
                reused.append((info.package, info.version))
            digest = sha256_file(upload.path)
            size = upload.path.stat().st_size
            filename = pool_filename(self.settings.component, info, digest)

            if info.key in seen_identity:
                raise PublicationError(
                    f"duplicate package in batch: {info.package} {info.version} "
                    f"({self.settings.architecture}); each package/version may "
                    "appear only once per publication"
                )
            if filename in seen_path and seen_path[filename] != digest:
                raise PublicationError(
                    f"package filename collision for {upload.original_name}"
                )
            seen_identity[info.key] = digest
            seen_path[filename] = digest
            uploads_by_dest[filename] = upload
            pairs.append(
                (
                    info,
                    PackageEntry(
                        package=info.package,
                        version=info.version,
                        architecture=info.architecture,
                        filename=filename,
                        sha256=digest,
                        size=size,
                    ),
                )
            )

        index = render_packages(pairs)
        index_gz = gzip_bytes(index)
        index_sha = sha256_hex(index_gz)
        plain_sha = sha256_hex(index)
        release = render_release(
            origin=self.settings.origin,
            label=self.settings.label,
            suite=self.settings.distribution,
            component=self.settings.component,
            architecture=self.settings.architecture,
            description=self.settings.description,
            indexes=[
                (plain_sha, len(index), self.settings.plain_index_relpath),
                (index_sha, len(index_gz), self.settings.index_relpath),
            ],
        )
        return PreparedPublication(
            pairs=tuple(pairs),
            index=index,
            index_gz=index_gz,
            release=release,
            index_sha256=index_sha,
            index_size=len(index_gz),
            plain_index_sha256=plain_sha,
            plain_index_size=len(index),
            uploads=uploads_by_dest,
            reused=tuple(reused),
        )

    def _file_plan(self, prepared: PreparedPublication) -> list[tuple[str, str]]:
        s = self.settings
        plan: list[tuple[str, str]] = []
        for _info, entry in prepared.pairs:
            plan.append((entry.filename, "pool"))
        plan.append(
            (f"{s.index_by_hash_prefix}/{prepared.plain_index_sha256}", "index_by_hash")
        )
        plan.append(
            (f"{s.index_by_hash_prefix}/{prepared.index_sha256}", "index_by_hash")
        )
        plan.append((s.plain_index_path, "index"))
        plan.append((s.index_path, "index"))
        return plan

    def _commit(self, prepared: PreparedPublication) -> Publication:
        s = self.settings
        pairs = prepared.pairs
        index = prepared.index
        index_gz = prepared.index_gz
        release = prepared.release
        index_sha = prepared.index_sha256
        plain_sha = prepared.plain_index_sha256
        release_sha = sha256_hex(release)
        created_at = _utcnow()
        uploads_by_dest = prepared.uploads

        plan = self._file_plan(prepared)
        plan.append((f"{s.release_by_hash_prefix}/{release_sha}", "release_by_hash"))
        plan.append((s.release_path, "release"))

        packages_json = json.dumps([entry.to_dict() for _i, entry in pairs])

        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO publications (created_at, state, release_sha256, "
                "index_sha256, plain_index_sha256, index_size, package_count, "
                "packages_json) VALUES (?, 'staged', ?, ?, ?, ?, ?, ?)",
                (
                    created_at,
                    release_sha,
                    index_sha,
                    plain_sha,
                    len(index_gz),
                    len(pairs),
                    packages_json,
                ),
            )
            publication_id = int(cursor.lastrowid)
            db.executemany(
                "INSERT INTO publication_files (publication_id, path, role) "
                "VALUES (?, ?, ?)",
                [(publication_id, path, role) for path, role in plan],
            )
            # Remember freshly validated data archives now.  The rows are not
            # reusable until this publication reaches 'committed'; if the
            # commit below fails, the publication (and these rows) stay
            # invisible to find_validated() and are rebound or superseded by
            # a later successful publication.
            self._remember_validated(db, publication_id, created_at, pairs)

        try:
            # 1. Immutable, content-addressed payload and by-hash indexes.
            for _info, entry in pairs:
                staged = uploads_by_dest[entry.filename]
                _atomic_move(staged.path, self.export / entry.filename)
            atomic_write(self.export / f"{s.index_by_hash_prefix}/{plain_sha}", index)
            atomic_write(self.export / f"{s.index_by_hash_prefix}/{index_sha}", index_gz)
            atomic_write(self.export / f"{s.release_by_hash_prefix}/{release_sha}", release)

            # 2. Fixed index paths (clients without by-hash support still see
            #    a consistent index; old clients are covered by by-hash).
            atomic_write(self.export / s.plain_index_path, index)
            atomic_write(self.export / s.index_path, index_gz)

            # 3. Commit point: the Release pointer.
            atomic_write(self.export / s.release_path, release)
            _fsync_dir(self.release_abspath.parent)
        except BaseException as exc:
            self._abort(publication_id)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, PublicationError):
                raise
            raise PublicationError(f"publication failed: {exc}") from exc

        with self._connect() as db:
            db.execute(
                "UPDATE publications SET state='committed' WHERE id=?",
                (publication_id,),
            )
        self._garbage_collect()

        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM publications WHERE id=?", (publication_id,)
            ).fetchone()
        assert row is not None
        return self._row_to_publication(row)

    def _abort(self, publication_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE publications SET state='aborted', error=? "
                "WHERE id=? AND state='staged'",
                ("interrupted before commit", publication_id),
            )
        # The Release may or may not have been swapped; either way the fixed
        # paths are reconciled against the latest committed publication.
        self._restore_fixed_paths()
        self._garbage_collect()

    # -------------------------------------------------------------- recovery
    def reconcile(self) -> None:
        """Finish or roll back publications interrupted by a crash/restart."""

        with self._exclusive():
            live_sha = (
                sha256_file(self.release_abspath)
                if self.release_abspath.exists()
                else None
            )
            with self._connect() as db:
                staged = db.execute(
                    "SELECT * FROM publications WHERE state='staged' ORDER BY id"
                ).fetchall()
            for row in staged:
                if live_sha is not None and live_sha == row["release_sha256"]:
                    # Release rename completed before the crash: the files were
                    # fsynced beforehand, so the publication is durable.
                    with self._connect() as db:
                        db.execute(
                            "UPDATE publications SET state='committed' WHERE id=?",
                            (row["id"],),
                        )
                else:
                    with self._connect() as db:
                        db.execute(
                            "UPDATE publications SET state='aborted', error=? "
                            "WHERE id=? AND state='staged'",
                            ("interrupted before commit", row["id"]),
                        )
            self._restore_fixed_paths()
            self._garbage_collect()
            cleanup_incoming(self.incoming)

    def _latest_committed(self, db: sqlite3.Connection) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM publications WHERE state='committed' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def _restore_fixed_paths(self) -> None:
        """Make the fixed index/Release match the latest committed publication."""

        s = self.settings
        with self._connect() as db:
            latest = self._latest_committed(db)
            if latest is None:
                return
        fixed = (
            (s.plain_index_path, latest["plain_index_sha256"]),
            (s.index_path, latest["index_sha256"]),
        )
        for relpath, digest in fixed:
            source = self.export / f"{s.index_by_hash_prefix}/{digest}"
            target = self.export / relpath
            if source.exists() and _file_sha(target) != digest:
                atomic_write(target, source.read_bytes())
        release_sha = latest["release_sha256"]
        release_src = self.export / f"{s.release_by_hash_prefix}/{release_sha}"
        release_dst = self.export / s.release_path
        if release_src.exists() and _file_sha(release_dst) != release_sha:
            atomic_write(release_dst, release_src.read_bytes())

    # ------------------------------------------------------------- file reads
    def _fixed_path_target(self, relpath: str) -> tuple[Path, str] | None:
        """Map a fixed (mutable) path to the content-addressed file for the
        *latest committed* publication.

        Serving these paths from the journal rather than from whatever the
        on-disk copy currently contains means Release and Packages always
        describe the same publication, even in the microseconds after the
        Release rename and even if a previous publication died midway.
        """

        s = self.settings
        if relpath not in (s.release_path, s.index_path, s.plain_index_path):
            return None
        with self._connect() as db:
            latest = self._latest_committed(db)
            if latest is None:
                return None
            if relpath == s.release_path:
                return (
                    self.export / f"{s.release_by_hash_prefix}/{latest['release_sha256']}",
                    latest["release_sha256"],
                )
            if relpath == s.index_path:
                digest = latest["index_sha256"]
            else:
                digest = latest["plain_index_sha256"]
            return self.export / f"{s.index_by_hash_prefix}/{digest}", digest

    def open_file(self, relpath: str) -> FileLease | None:
        """Open a published file for serving.

        Returns ``None`` when the path does not exist or is not a regular file.
        The returned lease must be closed by the caller.
        """

        if not relpath or relpath.startswith("/") or "\x00" in relpath:
            return None
        parts = relpath.split("/")
        if any(part in ("", ".", "..") for part in parts):
            return None
        expected_digest: str | None = None
        fixed = self._fixed_path_target(relpath)
        if fixed is not None:
            candidate, expected_digest = fixed
        else:
            candidate = self.export.joinpath(*parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.export)
        except (OSError, ValueError):
            return None
        try:
            fh = resolved.open("rb")
        except OSError:
            return None
        try:
            stat_result = os.fstat(fh.fileno())
            if not stat.S_ISREG(stat_result.st_mode):
                fh.close()
                return None
            etag = f'"{expected_digest}"' if expected_digest else f'"{stat_result.st_mtime_ns:x}-{stat_result.st_size:x}"'
            return FileLease(
                fh=fh,
                size=stat_result.st_size,
                relpath=relpath,
                last_modified=stat_result.st_mtime,
                etag=etag,
            )
        except OSError:
            fh.close()
            return None

    # -------------------------------------------------------------------- GC
    def _garbage_collect(self) -> None:
        s = self.settings
        with self._connect() as db:
            rows = db.execute(
                "SELECT id FROM publications WHERE state='committed' "
                "ORDER BY id DESC LIMIT ?",
                (s.retention,),
            ).fetchall()
            keep_ids = [row["id"] for row in rows]
            keep: set[Path] = set()
            if keep_ids:
                placeholders = ",".join("?" * len(keep_ids))
                for row in db.execute(
                    f"SELECT path FROM publication_files WHERE publication_id IN ({placeholders})",
                    keep_ids,
                ).fetchall():
                    keep.add(self.export / row["path"])

        removable_roots = [
            self.export / "pool",
            self.export / f"dists/{s.distribution}/by-hash",
            self.export / f"dists/{s.distribution}/{s.index_dir}/by-hash",
        ]
        for root in removable_roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if path.is_dir():
                    with contextlib.suppress(OSError):
                        path.rmdir()
                    continue
                if path in keep:
                    continue
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
        # Remove temporary files left behind by an interrupted atomic write.
        for tmp in self.export.rglob(".*.tmp"):
            if tmp.is_file():
                with contextlib.suppress(OSError):
                    tmp.unlink()

    # ------------------------------------------------------------------ misc
    def status(self) -> dict[str, object]:
        s = self.settings
        current = self.current_publication()
        return {
            "distribution": s.distribution,
            "component": s.component,
            "architecture": s.architecture,
            "retention": s.retention,
            "sources_list": s.sources_line,
            "release_url": f"/{s.release_path}",
            "current": current.to_dict() if current else None,
            "publications": [p.to_dict() for p in self.list_publications(20)],
        }


def _file_sha(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except FileNotFoundError:
        return None
    except OSError as exc:  # pragma: no cover - defensive
        if exc.errno == errno.ENOENT:
            return None
        raise


def cleanup_incoming(incoming: Path) -> None:
    """Remove any leftover intake directories (no publication owns them)."""

    if not incoming.exists():
        return
    for entry in incoming.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                entry.unlink()
