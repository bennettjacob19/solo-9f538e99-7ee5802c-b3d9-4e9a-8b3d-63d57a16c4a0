"""End-to-end tests for publication atomicity, by-hash retention and recovery."""

from __future__ import annotations

import gzip
import hashlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from debbuild import build_deb, make_data_tar  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent))
from app import deb as debmod  # noqa: E402
from app import storage  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage import Repository, StagedUpload  # noqa: E402


def make_settings(tmp_path: Path, retention: int = 2) -> Settings:
    return Settings(data_dir=tmp_path / "data", retention=retention)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_release(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()
    return result


def release_index_hash(text: str) -> tuple[str, str]:
    """Return the hash of the gzipped index listed in Release."""

    in_sha = False
    for line in text.splitlines():
        if line.startswith("SHA256:"):
            in_sha = True
            continue
        if in_sha and line.startswith(" "):
            digest, _size, path = line.split()
            if path.endswith("Packages.gz"):
                return digest, path
            continue
        if in_sha:
            break
    raise AssertionError("Release has no Packages.gz entry")


def release_paths(text: str) -> dict[str, str]:
    paths: dict[str, str] = {}
    in_sha = False
    for line in text.splitlines():
        if line.startswith("SHA256:"):
            in_sha = True
            continue
        if in_sha and line.startswith(" "):
            digest, _size, path = line.split()
            paths[path] = digest
            continue
        if in_sha:
            break
    return paths


def packages_entries(data: bytes) -> list[dict[str, str]]:
    text = gzip.decompress(data).decode()
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    last_key = None
    for line in text.splitlines():
        if not line.strip():
            if current:
                stanzas.append(current)
                current = {}
            last_key = None
            continue
        if line.startswith((" ", "\t")):
            assert last_key
            current[last_key] += "\n" + line
            continue
        key, value = line.split(":", 1)
        last_key = key
        current[key] = value.strip()
    if current:
        stanzas.append(current)
    return stanzas


@pytest.fixture()
def client(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client, settings


def upload(client: TestClient, paths: list[Path]):
    files = [
        ("files", (path.name, path.read_bytes(), "application/vnd.debian.binary-package"))
        for path in paths
    ]
    return client.post("/api/publish", files=files)


def make_batch(directory: Path, version: str = "1.0.0", names=("alpha", "beta")) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    out = []
    for name in names:
        path = directory / f"{name}_{version}_amd64.deb"
        build_deb(
            path,
            control={"Package": name, "Version": version},
            payload=f"{name}-{version}\n".encode(),
        )
        out.append(path)
    return out


def fetch_release(client: TestClient) -> str:
    response = client.get("/dists/stable/Release")
    assert response.status_code == 200
    return response.text


def fetch_index_by_hash(client: TestClient, release_text: str) -> tuple[bytes, str]:
    digest, path = release_index_hash(release_text)
    by_hash = f"/dists/stable/{Path(path).parent.name}/binary-amd64/by-hash/SHA256/{digest}"
    # path is "main/binary-amd64/Packages.gz"
    by_hash = f"/dists/stable/main/binary-amd64/by-hash/SHA256/{digest}"
    response = client.get(by_hash)
    assert response.status_code == 200, by_hash
    return response.content, digest


def test_publish_creates_release_and_by_hash(client):
    test_client, settings = client
    batch = make_batch(settings.data_dir.parent / "src")
    response = upload(test_client, batch)
    assert response.status_code == 201, response.text
    body = response.json()["published"]
    assert body["state"] == "committed"
    assert body["package_count"] == 2

    release = fetch_release(test_client)
    digest, path = release_index_hash(release)
    assert path == "main/binary-amd64/Packages.gz"
    index = test_client.get(f"/dists/stable/{path}").content
    assert sha256(index) == digest

    by_hash = test_client.get(
        f"/dists/stable/main/binary-amd64/by-hash/SHA256/{digest}"
    )
    assert by_hash.status_code == 200
    assert by_hash.content == index

    entries = packages_entries(index)
    assert {entry["Package"] for entry in entries} == {"alpha", "beta"}
    for entry in entries:
        package = test_client.get(f"/{entry['Filename']}")
        assert package.status_code == 200
        assert sha256(package.content) == entry["SHA256"]
        assert len(package.content) == int(entry["Size"])


def test_old_release_still_resolves_after_new_publication(client):
    test_client, settings = client
    src = settings.data_dir.parent / "src"
    assert upload(test_client, make_batch(src, "1.0.0")).status_code == 201
    old_release = fetch_release(test_client)
    old_index, old_digest = fetch_index_by_hash(test_client, old_release)
    old_entries = packages_entries(old_index)

    # Same package names, new version: files differ.
    assert upload(test_client, make_batch(src, "2.0.0")).status_code == 201
    new_release = fetch_release(test_client)
    assert new_release != old_release
    new_index, new_digest = fetch_index_by_hash(test_client, new_release)
    assert new_digest != old_digest

    # A client holding the previous Release can fetch the old index by hash...
    old_by_hash = test_client.get(
        f"/dists/stable/main/binary-amd64/by-hash/SHA256/{old_digest}"
    )
    assert old_by_hash.status_code == 200
    assert old_by_hash.content == old_index
    assert sha256(old_by_hash.content) == old_digest

    # ...and every package referenced by that old index.
    for entry in old_entries:
        package = test_client.get(f"/{entry['Filename']}")
        assert package.status_code == 200, entry["Filename"]
        assert sha256(package.content) == entry["SHA256"]


def test_same_name_and_version_republished_keeps_old_packages(client):
    test_client, settings = client
    src = settings.data_dir.parent / "src"

    v1 = src / "v1"
    v1.mkdir(parents=True)
    build_deb(
        v1 / "same_1.0.0_amd64.deb",
        control={"Package": "same", "Version": "1.0.0"},
        payload=b"first build\n",
    )
    assert upload(test_client, [v1 / "same_1.0.0_amd64.deb"]).status_code == 201
    old_release = fetch_release(test_client)
    old_index, old_digest = fetch_index_by_hash(test_client, old_release)
    old_entry = packages_entries(old_index)[0]

    v2 = src / "v2"
    v2.mkdir(parents=True)
    build_deb(
        v2 / "same_1.0.0_amd64.deb",
        control={"Package": "same", "Version": "1.0.0"},
        payload=b"second build, different bytes\n",
    )
    assert upload(test_client, [v2 / "same_1.0.0_amd64.deb"]).status_code == 201
    new_release = fetch_release(test_client)
    new_index, new_digest = fetch_index_by_hash(test_client, new_release)
    new_entry = packages_entries(new_index)[0]

    assert old_digest != new_digest
    assert old_entry["Filename"] != new_entry["Filename"]
    assert old_entry["SHA256"] != new_entry["SHA256"]

    old_index_again = test_client.get(
        f"/dists/stable/main/binary-amd64/by-hash/SHA256/{old_digest}"
    )
    assert old_index_again.content == old_index
    old_package = test_client.get(f"/{old_entry['Filename']}")
    assert old_package.status_code == 200
    assert sha256(old_package.content) == old_entry["SHA256"]
    new_package = test_client.get(f"/{new_entry['Filename']}")
    assert new_package.status_code == 200
    assert sha256(new_package.content) == new_entry["SHA256"]


def test_only_two_successful_releases_are_retained(client):
    test_client, settings = client
    src = settings.data_dir.parent / "src"
    releases = []
    indexes = {}
    for version in ("1.0.0", "2.0.0", "3.0.0"):
        assert upload(test_client, make_batch(src, version)).status_code == 201
        release = fetch_release(test_client)
        releases.append(release)
        indexes[version] = release_index_hash(release)[0]

    # Oldest release's index hash is gone from by-hash and its packages too.
    old = indexes["1.0.0"]
    assert test_client.get(
        f"/dists/stable/main/binary-amd64/by-hash/SHA256/{old}"
    ).status_code == 404
    # The two newest publications remain.
    for version in ("2.0.0", "3.0.0"):
        digest = indexes[version]
        response = test_client.get(
            f"/dists/stable/main/binary-amd64/by-hash/SHA256/{digest}"
        )
        assert response.status_code == 200

    status = test_client.get("/api/status").json()
    committed = [p for p in status["publications"] if p["state"] == "committed"]
    assert len(committed) == 3  # journal history is kept
    assert settings.retention == 2


def test_retention_keeps_packages_of_previous_release_only(client):
    test_client, settings = client
    src = settings.data_dir.parent / "src"
    assert upload(test_client, make_batch(src, "1.0.0")).status_code == 201
    rel1 = fetch_release(test_client)
    idx1, _ = fetch_index_by_hash(test_client, rel1)
    entries1 = packages_entries(idx1)

    assert upload(test_client, make_batch(src, "2.0.0")).status_code == 201
    assert upload(test_client, make_batch(src, "3.0.0")).status_code == 201

    for entry in entries1:
        assert test_client.get(f"/{entry['Filename']}").status_code == 404


def test_bad_package_leaves_repository_untouched(client):
    test_client, settings = client
    src = settings.data_dir.parent / "src"
    assert upload(test_client, make_batch(src, "1.0.0")).status_code == 201
    release_before = fetch_release(test_client)
    index_hash_before = release_index_hash(release_before)[0]

    bad = settings.data_dir.parent / "bad" / "broken_1.0.0_amd64.deb"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a deb at all")
    response = upload(test_client, make_batch(src, "9.9.9") + [bad])
    assert response.status_code == 400
    assert fetch_release(test_client) == release_before
    assert test_client.get(
        f"/dists/stable/main/binary-amd64/by-hash/SHA256/{index_hash_before}"
    ).status_code == 200


def test_wrong_architecture_rejected(client):
    test_client, settings = client
    path = settings.data_dir.parent / "arm.deb"
    build_deb(
        path,
        control={"Package": "armpkg", "Version": "1.0.0"},
        arch="arm64",
    )
    response = upload(test_client, [path])
    assert response.status_code == 400
    assert "arm64" in response.text
    assert test_client.get("/dists/stable/Release").status_code == 404


def test_duplicate_version_in_batch_rejected(client):
    test_client, settings = client
    src = settings.data_dir.parent / "dup"
    src.mkdir()
    first = src / "x1.deb"
    second = src / "x2.deb"
    build_deb(first, control={"Package": "dup", "Version": "1.0.0"}, payload=b"a")
    build_deb(second, control={"Package": "dup", "Version": "1.0.0"}, payload=b"b")
    response = upload(test_client, [first, second])
    assert response.status_code == 400
    assert "duplicate" in response.text


def test_interrupted_publication_rolls_back(client, monkeypatch):
    test_client, settings = client
    src = settings.data_dir.parent / "src"
    assert upload(test_client, make_batch(src, "1.0.0")).status_code == 201
    release_before = fetch_release(test_client)

    original = storage.atomic_write

    def exploding(target: Path, data: bytes) -> None:
        if target.name == "Release":
            raise OSError("disk full")
        original(target, data)

    monkeypatch.setattr(storage, "atomic_write", exploding)
    response = upload(test_client, make_batch(src, "2.0.0"))
    assert response.status_code == 400
    monkeypatch.setattr(storage, "atomic_write", original)

    assert fetch_release(test_client) == release_before
    status = test_client.get("/api/status").json()
    assert [p["state"] for p in status["publications"]][0] == "aborted"
    # The interrupted batch's packages are not referenced and have been collected.
    assert test_client.get("/dists/stable/main/binary-amd64/by-hash/SHA256/" +
                           release_index_hash(release_before)[0]).status_code == 200


def test_crash_between_rename_and_commit_is_committed_on_restart(tmp_path):
    """Simulate death after the Release rename but before the DB update."""

    settings = make_settings(tmp_path)
    repo = Repository(settings)
    src = tmp_path / "src"
    src.mkdir()
    uploads = [
        StagedUpload(path=path, original_name=path.name)
        for path in make_batch(src, "1.0.0")
    ]
    # Copy into incoming as the API would.
    intake = repo.incoming / "batch1"
    intake.mkdir()
    staged = []
    for upload in uploads:
        target = intake / upload.path.name
        target.write_bytes(upload.path.read_bytes())
        staged.append(StagedUpload(path=target, original_name=upload.original_name))

    prepared = repo._prepare(staged)
    with repo._exclusive():
        publication = repo._commit(prepared)
    assert publication.state == "committed"

    # Now emulate: new staged row exists on disk and Release already renamed.
    prepared2 = repo._prepare(
        [
            StagedUpload(
                path=_write(tmp_path / "b2", "newpkg", "2.0.0"),
                original_name="newpkg.deb",
            )
        ]
    )
    with repo._connect() as db:
        cursor = db.execute(
            "INSERT INTO publications (created_at, state, release_sha256, "
            "index_sha256, plain_index_sha256, index_size, package_count, "
            "packages_json) VALUES ('now', 'staged', ?, ?, ?, 0, 0, '[]')",
            (
                sha256(prepared2.release),
                prepared2.index_sha256,
                prepared2.plain_index_sha256,
            ),
        )
        staged_id = cursor.lastrowid
    # Simulate the commit point having happened (Release rename) but not the DB.
    repo.release_abspath.write_bytes(prepared2.release)
    # ...and crash. A fresh Repository instance must reconcile.
    recovered = Repository(settings)
    with recovered._connect() as db:
        row = db.execute(
            "SELECT state FROM publications WHERE id=?", (staged_id,)
        ).fetchone()
    assert row["state"] == "committed"
    assert recovered.release_abspath.read_bytes() == prepared2.release


def test_crash_before_rename_is_aborted_on_restart(tmp_path):
    settings = make_settings(tmp_path)
    repo = Repository(settings)
    src = tmp_path / "src"
    src.mkdir()
    batch = make_batch(src, "1.0.0")
    intake = repo.incoming / "batch1"
    intake.mkdir()
    staged = []
    for path in batch:
        target = intake / path.name
        target.write_bytes(path.read_bytes())
        staged.append(StagedUpload(path=target, original_name=path.name))
    prepared = repo._prepare(staged)
    with repo._connect() as db:
        cursor = db.execute(
            "INSERT INTO publications (created_at, state, release_sha256, "
            "index_sha256, plain_index_sha256, index_size, package_count, "
            "packages_json) VALUES ('now', 'staged', ?, ?, ?, 0, 0, '[]')",
            (
                sha256(prepared.release),
                prepared.index_sha256,
                prepared.plain_index_sha256,
            ),
        )
        staged_id = cursor.lastrowid
    # No files were written yet.
    resumed = Repository(settings)
    with resumed._connect() as db:
        row = db.execute(
            "SELECT state FROM publications WHERE id=?", (staged_id,)
        ).fetchone()
    assert row["state"] == "aborted"
    assert not resumed.release_abspath.exists()
    # The repository still accepts new publications afterwards.
    ok = make_batch(tmp_path / "ok", "1.0.0")
    (tmp_path / "ok").mkdir(exist_ok=True)
    publication, _reused = resumed.publish(
        [StagedUpload(path=p, original_name=p.name) for p in ok]
    )
    assert publication.state == "committed"


def _write(directory: Path, name: str, version: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.deb"
    build_deb(path, control={"Package": name, "Version": version}, payload=version.encode())
    return path


def test_head_and_path_traversal(client):
    test_client, settings = client
    assert upload(test_client, make_batch(settings.data_dir.parent / "src")).status_code == 201
    head = test_client.head("/dists/stable/Release")
    assert head.status_code == 200
    assert head.content == b""
    traversal = test_client.get("/../state.sqlite3")
    assert traversal.status_code in (404, 400)
    assert test_client.get("/dists/stable/by-hash/SHA256/nope").status_code == 404


def test_docs_page_renders(client):
    test_client, _settings = client
    response = test_client.get("/docs")
    assert response.status_code == 200
    assert "/api/publish" in response.text
    assert 'name="files" multiple' in response.text


def test_download_in_flight_survives_garbage_collection(client):
    """A package open for streaming must remain readable after eviction."""

    test_client, settings = client
    src = settings.data_dir.parent / "src"
    assert upload(test_client, make_batch(src, "1.0.0")).status_code == 201
    index = test_client.get("/dists/stable/main/binary-amd64/Packages.gz").content
    entry = packages_entries(index)[0]

    repository: Repository = test_client.app.state.repository
    lease = repository.open_file(entry["Filename"])
    assert lease is not None

    # Three more publications evict the first one from disk.
    for version in ("2.0.0", "3.0.0", "4.0.0"):
        assert upload(test_client, make_batch(src, version)).status_code == 201
    assert test_client.get(f"/{entry['Filename']}").status_code == 404

    # The already-open descriptor still yields the exact original bytes.
    data = lease.fh.read()
    lease.close()
    assert sha256(data) == entry["SHA256"]


def test_release_advertises_by_hash_and_lists_both_indexes(client):
    test_client, settings = client
    assert upload(test_client, make_batch(settings.data_dir.parent / "src")).status_code == 201
    release = fetch_release(test_client)
    assert "Acquire-By-Hash: yes" in release
    paths = release_paths(release)
    assert set(paths) == {
        "main/binary-amd64/Packages",
        "main/binary-amd64/Packages.gz",
    }
    for path, digest in paths.items():
        by_hash = test_client.get(
            f"/dists/stable/main/binary-amd64/by-hash/SHA256/{digest}"
        )
        assert by_hash.status_code == 200
        assert sha256(by_hash.content) == digest
        plain = test_client.get(f"/dists/stable/{path}")
        assert plain.content == by_hash.content


def test_release_by_hash_is_served(client):
    test_client, settings = client
    body = upload(test_client, make_batch(settings.data_dir.parent / "src")).json()
    digest = body["published"]["release_sha256"]
    response = test_client.get(f"/dists/stable/by-hash/SHA256/{digest}")
    assert response.status_code == 200
    assert sha256(response.content) == digest


def test_status_reports_retention_and_publications(client):
    test_client, settings = client
    src = settings.data_dir.parent / "src"
    for version in ("1.0.0", "2.0.0"):
        assert upload(test_client, make_batch(src, version)).status_code == 201
    status = test_client.get("/api/status").json()
    assert status["retention"] == 2
    assert status["distribution"] == "stable"
    assert status["component"] == "main"
    assert status["architecture"] == "amd64"
    assert status["current"]["state"] == "committed"
    assert len(status["publications"]) == 2


@pytest.mark.parametrize(
    "mutate,expect",
    [
        (lambda blob: blob[: len(blob) // 2], "truncated"),
        (lambda blob: blob.replace(b"!<arch>\n", b"!<arch>X"), "ar archive header"),
        (lambda blob: b"", "empty"),
    ],
)
def test_structurally_broken_packages_rejected(client, mutate, expect):
    test_client, settings = client
    good = settings.data_dir.parent / "good" / "x.deb"
    build_deb(good, control={"Package": "broken", "Version": "1.0"})
    bad = settings.data_dir.parent / "bad" / "x.deb"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(mutate(good.read_bytes()))
    response = upload(test_client, [bad])
    assert response.status_code == 400
    assert expect in response.text.lower()
    assert test_client.get("/dists/stable/Release").status_code == 404


def test_corrupt_payload_inside_valid_container_rejected(client):
    test_client, settings = client
    good = settings.data_dir.parent / "good" / "y.deb"
    build_deb(good, control={"Package": "corrupt", "Version": "1.0"}, payload=b"A" * 100000)
    blob = bytearray(good.read_bytes())
    # Flip bytes inside the data archive rather than in the ar headers.
    start = blob.find(b"\x1f\x8b\x08", 200)
    assert start > 0
    for offset in range(start + 30, start + 60):
        blob[offset] ^= 0xFF
    bad = settings.data_dir.parent / "bad" / "y.deb"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(blob)
    response = upload(test_client, [bad])
    assert response.status_code == 400
    assert "corrupt" in response.text.lower() or "truncat" in response.text.lower()


def test_packages_index_is_well_formed(client):
    test_client, settings = client
    payload = settings.data_dir.parent / "p" / "pkg.deb"
    build_deb(
        payload,
        control={
            "Package": "libexample-dev",
            "Version": "1:2.3.4-5",
            "Depends": "libc6 (>= 2.34), libfoo | libbar",
            "Description": "long description\n with continuation\n .\n and more",
        },
    )
    assert upload(test_client, [payload]).status_code == 201
    data = test_client.get("/dists/stable/main/binary-amd64/Packages.gz").content
    entries = packages_entries(data)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["Package"] == "libexample-dev"
    assert entry["Version"] == "1:2.3.4-5"
    assert entry["Architecture"] == "amd64"
    assert entry["Depends"] == "libc6 (>= 2.34), libfoo | libbar"
    assert "with continuation" in entry["Description"]
    assert entry["SHA256"] == sha256(
        test_client.get(f"/{entry['Filename']}").content
    )


def test_fixed_paths_serve_latest_committed_publication(client):
    """Release and Packages at their fixed URLs always agree.

    Even if a stale fixed-path copy is left behind (e.g. an interrupted
    earlier run), both URLs resolve through the journal to the same
    publication.
    """

    test_client, settings = client
    src = settings.data_dir.parent / "src"
    assert upload(test_client, make_batch(src, "1.0.0")).status_code == 201
    repository: Repository = test_client.app.state.repository
    current = repository.current_publication()
    assert current is not None

    # Corrupt the on-disk copies: serving must not be affected.
    (repository.export / settings.release_path).write_bytes(b"stale release\n")
    (repository.export / settings.index_path).write_bytes(b"stale index\n")
    (repository.export / settings.plain_index_path).write_bytes(b"stale plain\n")

    release = fetch_release(test_client)
    digest, path = release_index_hash(release)
    index = test_client.get(f"/dists/stable/{path}")
    assert sha256(index.content) == digest
    plain = test_client.get("/dists/stable/main/binary-amd64/Packages")
    assert sha256(plain.content) == release_paths(release)["main/binary-amd64/Packages"]
    assert "stale" not in release


def test_head_matches_get_for_repository_files(client):
    test_client, settings = client
    assert upload(test_client, make_batch(settings.data_dir.parent / "src")).status_code == 201
    for path in (
        "/dists/stable/Release",
        "/dists/stable/main/binary-amd64/Packages",
        "/dists/stable/main/binary-amd64/Packages.gz",
    ):
        get = test_client.get(path)
        head = test_client.head(path)
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-length"] == str(len(get.content))
        assert head.headers["etag"] == get.headers["etag"]


# ---------------------------------------------------------------------------
# Data archive validation cache: only Depends/Version changed, same data.tar
# ---------------------------------------------------------------------------

def _shared_data_deb(path: Path, *, package: str, version: str, depends: str | None,
                     data_tar: bytes) -> Path:
    control = {"Package": package, "Version": version}
    if depends is not None:
        control["Depends"] = depends
    return build_deb(path, control=control, data_tar=data_tar)


def test_same_data_archive_skips_revalidation_and_response_lists_it(client, monkeypatch):
    test_client, settings = client
    src = settings.data_dir.parent / "src"
    data_tar = make_data_tar()

    calls = {"count": 0}
    original_validate = debmod._validate_payload

    def counting_validate(*args, **kwargs):
        calls["count"] += 1
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(debmod, "_validate_payload", counting_validate)

    # First publication: full validation, nothing reusable yet.
    first = src / "relint_1.0.0_amd64.deb"
    _shared_data_deb(
        first, package="relint", version="1.0.0", depends="libc6 (>= 2.31)",
        data_tar=data_tar,
    )
    response = upload(test_client, [first])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reused_checksums"] == []
    assert calls["count"] == 1

    # Second upload: identical data archive, new Version and Depends. Control
    # inspection still runs (format/arch/duplicate checks are unchanged), but
    # the data archive must not be decompressed again.
    second = src / "relint_2.0.0_amd64.deb"
    _shared_data_deb(
        second, package="relint", version="2.0.0",
        depends="libc6 (>= 2.34), libfoo | libbar", data_tar=data_tar,
    )
    response = upload(test_client, [second])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reused_checksums"] == [
        {"package": "relint", "version": "2.0.0"}
    ]
    assert calls["count"] == 1  # still only the first package was fully validated

    # The new index must carry the new version/depends, and the downloaded
    # package's whole-file SHA-256 must match the index.
    index = test_client.get("/dists/stable/main/binary-amd64/Packages.gz").content
    entries = packages_entries(index)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["Package"] == "relint"
    assert entry["Version"] == "2.0.0"
    assert entry["Depends"] == "libc6 (>= 2.34), libfoo | libbar"
    downloaded = test_client.get(f"/{entry['Filename']}")
    assert downloaded.status_code == 200
    assert sha256(downloaded.content) == entry["SHA256"]
    assert int(entry["Size"]) == len(downloaded.content) == second.stat().st_size


def test_two_packages_in_one_batch_reuse_the_same_data_archive(client):
    test_client, settings = client
    src = settings.data_dir.parent / "src"
    data_tar = make_data_tar()

    # Prime the cache with the shared data archive.
    prime = src / "prime_1.0.0_amd64.deb"
    _shared_data_deb(
        prime, package="prime", version="1.0.0", depends=None, data_tar=data_tar
    )
    assert upload(test_client, [prime]).status_code == 201

    # A batch of two different packages (different name/version/depends) whose
    # data archive bytes are identical must reuse the record for both.
    a = src / "reuse-a_3.1.0_amd64.deb"
    b = src / "reuse-b_4.2.0_amd64.deb"
    _shared_data_deb(
        a, package="reuse-a", version="3.1.0", depends="libc6", data_tar=data_tar
    )
    _shared_data_deb(
        b, package="reuse-b", version="4.2.0", depends="zlib1g, libc6",
        data_tar=data_tar,
    )
    response = upload(test_client, [a, b])
    assert response.status_code == 201, response.text
    reused = sorted(
        (item["package"], item["version"])
        for item in response.json()["reused_checksums"]
    )
    assert reused == [("reuse-a", "3.1.0"), ("reuse-b", "4.2.0")]

    index = test_client.get("/dists/stable/main/binary-amd64/Packages.gz").content
    entries = {e["Package"]: e for e in packages_entries(index)}
    assert set(entries) == {"reuse-a", "reuse-b"}
    assert entries["reuse-a"]["Version"] == "3.1.0"
    assert entries["reuse-a"]["Depends"] == "libc6"
    assert entries["reuse-b"]["Version"] == "4.2.0"
    assert entries["reuse-b"]["Depends"] == "zlib1g, libc6"
    for entry in entries.values():
        package = test_client.get(f"/{entry['Filename']}")
        assert sha256(package.content) == entry["SHA256"]


def test_validation_record_survives_gc_and_restart(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    repo = Repository(settings)
    src = tmp_path / "src"
    src.mkdir()
    data_tar = make_data_tar()

    first = src / "ever_1.0.0_amd64.deb"
    _shared_data_deb(
        first, package="ever", version="1.0.0", depends="libc6", data_tar=data_tar
    )
    first_digest = deb_archive_sha256(first)
    publication, reused = repo.publish(
        [StagedUpload(path=first, original_name=first.name)]
    )
    assert publication.state == "committed"
    assert reused == ()

    # Evict the original package from the pool through further publications:
    # retention is 2, so three later releases remove release 1's files.
    for version in ("2.0.0", "3.0.0", "4.0.0"):
        other = src / f"other_{version}_amd64.deb"
        build_deb(
            other,
            control={"Package": "other", "Version": version},
            payload=version.encode(),
        )
        repo.publish([StagedUpload(path=other, original_name=other.name)])

    pool = list((repo.export / "pool").rglob("*.deb"))
    assert all("ever" not in str(path) for path in pool)

    # A brand new process (new Repository instance, same sqlite journal) must
    # still recognise the data archive even though the package was collected.
    reloaded = Repository(settings)
    assert reloaded.find_validated("data.tar.gz", first_digest)

    validate_calls = {"count": 0}
    original_validate = debmod._validate_payload

    def counting_validate(*args, **kwargs):
        validate_calls["count"] += 1
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(debmod, "_validate_payload", counting_validate)
    rebuilt = src / "ever_5.0.0_amd64.deb"
    _shared_data_deb(
        rebuilt, package="ever", version="5.0.0", depends="libc6, libnew",
        data_tar=data_tar,
    )
    publication, reused = reloaded.publish(
        [StagedUpload(path=rebuilt, original_name=rebuilt.name)]
    )
    assert publication.state == "committed"
    assert reused == (("ever", "5.0.0"),)
    assert validate_calls["count"] == 0


def test_interrupted_publication_leaves_no_reusable_record(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    repo = Repository(settings)
    src = tmp_path / "src"
    src.mkdir()
    data_tar = make_data_tar()

    # A publication that dies at the Release commit point must not grant reuse.
    original = storage.atomic_write

    def exploding(target: Path, data: bytes) -> None:
        if target.name == "Release":
            raise OSError("disk full")
        original(target, data)

    doomed = src / "doomed_1.0.0_amd64.deb"
    _shared_data_deb(
        doomed, package="doomed", version="1.0.0", depends="libc6",
        data_tar=data_tar,
    )
    digest = deb_archive_sha256(doomed)
    monkeypatch.setattr(storage, "atomic_write", exploding)
    with pytest.raises(storage.PublicationError):
        repo.publish([StagedUpload(path=doomed, original_name=doomed.name)])
    monkeypatch.setattr(storage, "atomic_write", original)

    assert repo.find_validated("data.tar.gz", digest) is False

    # Simulate a service restart: reconcile marks the row aborted, and the
    # validation record must stay invisible.
    recovered = Repository(settings)
    assert recovered.find_validated("data.tar.gz", digest) is False

    # The same data archive in a later, successful publication is fully
    # validated (no reuse from the aborted attempt) and then becomes reusable.
    validate_calls = {"count": 0}
    original_validate = debmod._validate_payload

    def counting_validate(*args, **kwargs):
        validate_calls["count"] += 1
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(debmod, "_validate_payload", counting_validate)
    good = src / "doomed_2.0.0_amd64.deb"
    _shared_data_deb(
        good, package="doomed", version="2.0.0", depends="libc6, libextra",
        data_tar=data_tar,
    )
    publication, reused = recovered.publish(
        [StagedUpload(path=good, original_name=good.name)]
    )
    assert publication.state == "committed"
    assert reused == ()
    assert validate_calls["count"] == 1
    assert recovered.find_validated("data.tar.gz", digest) is True

    # One more identical-archive upload now reuses the successful record.
    again = src / "doomed_3.0.0_amd64.deb"
    _shared_data_deb(
        again, package="doomed", version="3.0.0", depends="libc6, libextra",
        data_tar=data_tar,
    )
    publication, reused = recovered.publish(
        [StagedUpload(path=again, original_name=again.name)]
    )
    assert reused == (("doomed", "3.0.0"),)
    assert validate_calls["count"] == 1


def test_recovered_successful_publication_makes_record_reusable(tmp_path):
    """A staged publication reconciled as committed (rename happened) leaves
    a reusable validation record after restart."""

    settings = make_settings(tmp_path)
    repo = Repository(settings)
    src = tmp_path / "src"
    src.mkdir()
    data_tar = make_data_tar()
    first = src / "crashy_1.0.0_amd64.deb"
    _shared_data_deb(
        first, package="crashy", version="1.0.0", depends="libc6",
        data_tar=data_tar,
    )
    staged = [StagedUpload(path=first, original_name=first.name)]
    prepared = repo._prepare(staged)

    # Embed the checksum rows exactly as _commit would, then crash before the
    # state flips to committed - but with the Release already renamed, which is
    # reconcile()'s definition of a successful publication.
    with repo._connect() as db:
        cursor = db.execute(
            "INSERT INTO publications (created_at, state, release_sha256, "
            "index_sha256, plain_index_sha256, index_size, package_count, "
            "packages_json) VALUES ('now', 'staged', ?, ?, ?, ?, ?, '[]')",
            (
                sha256(prepared.release),
                prepared.index_sha256,
                prepared.plain_index_sha256,
                prepared.index_size,
                len(prepared.pairs),
            ),
        )
        staged_id = int(cursor.lastrowid)
        repo._remember_validated(db, staged_id, "now", prepared.pairs)
    repo.release_abspath.parent.mkdir(parents=True, exist_ok=True)
    repo.release_abspath.write_bytes(prepared.release)

    reloaded = Repository(settings)
    digest = deb_archive_sha256(first)
    assert reloaded.find_validated("data.tar.gz", digest) is True


def test_corrupt_data_archive_is_rejected_even_with_same_control(client):
    test_client, settings = client
    good = settings.data_dir.parent / "good" / "z.deb"
    build_deb(good, control={"Package": "corrupt2", "Version": "1.0"},
              payload=b"A" * 100000)
    assert upload(test_client, [good]).status_code == 201

    # Reuse the validated data archive's compression/digest in name only:
    # flip bytes inside the gzip stream. The cache lookup must miss because
    # the raw digest changes, and full validation must reject the package.
    blob = bytearray(good.read_bytes())
    start = blob.find(b"\x1f\x8b\x08", 200)
    assert start > 0
    for offset in range(start + 30, start + 60):
        blob[offset] ^= 0xFF
    bad = settings.data_dir.parent / "bad" / "z.deb"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(blob)
    response = upload(test_client, [bad])
    assert response.status_code == 400
    assert "corrupt" in response.text.lower() or "truncat" in response.text.lower()


def test_different_compression_same_tar_does_not_reuse(tmp_path):
    """The cache key includes the compression format: a data.tar.xz must not
    be treated as validated because a data.tar.gz with the same tar contents
    was seen earlier."""

    import lzma

    settings = make_settings(tmp_path)
    repo = Repository(settings)
    src = tmp_path / "src"
    src.mkdir()
    gz_archive = make_data_tar()
    gz_deb = src / "comp_1.0.0_amd64.deb"
    _shared_data_deb(
        gz_deb, package="comp", version="1.0.0", depends=None, data_tar=gz_archive
    )
    publication, reused = repo.publish(
        [StagedUpload(path=gz_deb, original_name=gz_deb.name)]
    )
    assert publication.state == "committed" and reused == ()

    # Build the xz variant from the same uncompressed tar payload.
    tar_blob = gzip.decompress(gz_archive)
    xz_archive = lzma.compress(tar_blob)
    xz_deb = src / "comp_2.0.0_amd64.deb"
    build_deb(
        xz_deb,
        control={"Package": "comp", "Version": "2.0.0"},
        data_tar=xz_archive,
        data_member="data.tar.xz",
    )
    assert repo.find_validated("data.tar.xz", sha256(xz_archive)) is False
    publication, reused = repo.publish(
        [StagedUpload(path=xz_deb, original_name=xz_deb.name)]
    )
    assert publication.state == "committed"
    assert reused == ()
    assert repo.find_validated("data.tar.xz", sha256(xz_archive)) is True


def deb_archive_sha256(deb_path: Path) -> str:
    """Compute the raw data.tar member digest the way the server does."""

    import io

    from app.deb import _read_ar_index

    size = deb_path.stat().st_size
    with deb_path.open("rb") as fh:
        entries = {
            name: (offset, member_size)
            for name, offset, member_size in _read_ar_index(
                io.BufferedReader(fh), size
            )
        }
    data_member = next(name for name in entries if name.startswith("data.tar"))
    offset, member_size = entries[data_member]
    return debmod.hash_raw_member(deb_path, offset, member_size)
