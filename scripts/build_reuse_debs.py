#!/usr/bin/env python3
"""Build two .deb files sharing one byte-identical data archive.

Used by scripts/e2e-apt.sh to prove the server-side validation cache:
the two packages differ only in control metadata (Version/Depends), while
their data.tar.gz members are the exact same bytes and therefore get the
same raw data-archive SHA-256.

Usage: build_reuse_debs.py <output-dir> <package> <version-a> <version-b>
"""

from __future__ import annotations

import gzip
import io
import sys
import tarfile
from pathlib import Path


def _tar_gz(members: dict[str, bytes], mtime: int = 0) -> bytes:
    # GzipFile with mtime=0 keeps the archive byte-deterministic across runs:
    # the e2e checks rely on the same digest being produced after a restart.
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                info.mtime = mtime
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        header = (
            f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n".encode()
        )
        out += header + data
        if len(data) % 2:
            out += b"\n"
    return bytes(out)


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    out_dir = Path(argv[1])
    package, version_a, version_b = argv[2], argv[3], argv[4]
    out_dir.mkdir(parents=True, exist_ok=True)

    # One data archive, embedded verbatim into both packages.
    data_tar = _tar_gz(
        {f"usr/share/doc/{package}/README": b"shared data archive\n"}
    )

    def build(version: str, depends: str) -> Path:
        control_text = (
            f"Package: {package}\n"
            f"Version: {version}\n"
            "Architecture: amd64\n"
            "Maintainer: Test <test@example.com>\n"
            "Section: misc\n"
            "Priority: optional\n"
            f"Depends: {depends}\n"
            f"Description: {package} reuse test {version}\n"
        ).encode()
        control_tar = _tar_gz({"control": control_text, "md5sums": b""})
        blob = _ar(
            [
                ("debian-binary", b"2.0\n"),
                ("control.tar.gz", control_tar),
                ("data.tar.gz", data_tar),
            ]
        )
        path = out_dir / f"{package}_{version}_amd64.deb"
        path.write_bytes(blob)
        return path

    path_a = build(version_a, "libc6 (>= 2.31)")
    path_b = build(version_b, "libc6 (>= 2.34), libfoo | libbar")
    print(path_a)
    print(path_b)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
