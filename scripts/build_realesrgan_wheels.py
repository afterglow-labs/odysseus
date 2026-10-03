#!/usr/bin/env python3
"""Build verified, pure-Python Real-ESRGAN dependency wheels on Python 3.14.

The released setup.py files use exec() followed by locals() to read a version.
PEP 667 makes that fail on Python 3.13+. Patch only that build-time lookup and
BasicSR's unnecessary native build requirements; runtime requirements remain
intact. Docker and the Cookbook installer use this same builder.

Without --install this only builds wheels. --install installs those three
wheels without dependencies into the interpreter running this script; the
caller then runs the original dependency installation command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
from typing import NamedTuple
import urllib.request
import zipfile


class Source(NamedTuple):
    name: str
    version: str
    url: str
    sha256: str


# Official PyPI release sdists. Updating a release requires reviewing its
# setup.py and recording the new published digest, not trusting a mutable URL.
SOURCES = (
    Source(
        "basicsr", "1.4.2",
        "https://files.pythonhosted.org/packages/86/41/00a6b000f222f0fa4c6d9e1d6dcc9811a374cabb8abb9d408b77de39648c/basicsr-1.4.2.tar.gz",
        "b89b595a87ef964cda9913b4d99380ddb6554c965577c0c10cb7b78e31301e87",
    ),
    Source(
        "gfpgan", "1.3.8",
        "https://files.pythonhosted.org/packages/6b/e9/b2db24ed840f188792581d217229022ff85e0ae3055a708e9f28430b8083/gfpgan-1.3.8.tar.gz",
        "21618b06ce8ea6230448cb526b012004f23a9ab956b55c833f69b9fc8a60c4f9",
    ),
    Source(
        "facexlib", "0.3.0",
        "https://files.pythonhosted.org/packages/e1/93/c820cd2c6315b635934770808e0b01ed4db257ec33bcf803909dcf4bce15/facexlib-0.3.0.tar.gz",
        "7ae784a520eb52e05583e8bf9f68f77f45083239ac754d646d635017b49e7763",
    ),
)
PATCH_REVISION = 1
MANIFEST_NAME = "realesrgan-compatibility-wheels.json"
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000


class BuildError(RuntimeError):
    """A source archive or built wheel did not satisfy the expected contract."""


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _fingerprint() -> dict:
    return {
        "patch_revision": PATCH_REVISION,
        "basicsr_extensions": False,
        "sources": {source.name: [source.version, source.sha256] for source in SOURCES},
    }


def default_output_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return base / "odysseus" / "realesrgan-wheels"


def _wheel_name(source: Source) -> str:
    return f"{source.name}-{source.version}-py3-none-any.whl"


def _cached_wheels(output_dir: Path) -> list[Path] | None:
    try:
        manifest = json.loads((output_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        if manifest["build"] != _fingerprint():
            return None
        wheels = []
        for source in SOURCES:
            filename = _wheel_name(source)
            wheel = output_dir / filename
            if _digest(wheel) != manifest["wheels"][filename]:
                return None
            wheels.append(wheel)
        return wheels
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _download(source: Source, destination: Path) -> None:
    print(f">> Fetching verified {source.name} {source.version} source", flush=True)
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(source.url, timeout=30) as response, destination.open("wb") as stream:
        while chunk := response.read(64 * 1024):
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                raise BuildError(f"{source.name}: source archive exceeds the download limit")
            digest.update(chunk)
            stream.write(chunk)
    if digest.hexdigest() != source.sha256:
        raise BuildError(f"{source.name}: source SHA256 does not match the pinned PyPI release")


def _extract(source: Source, archive: Path, destination: Path) -> Path:
    root_name = f"{source.name}-{source.version}"
    with tarfile.open(archive, "r:gz") as tar:
        members = []
        extracted_bytes = 0
        for member in tar:
            members.append(member)
            extracted_bytes += member.size
            if len(members) > MAX_ARCHIVE_MEMBERS or extracted_bytes > MAX_EXTRACTED_BYTES:
                raise BuildError(f"{source.name}: source archive exceeds extraction limits")
            path = PurePosixPath(member.name)
            if (
                path.is_absolute() or ".." in path.parts or "\\" in member.name
                or not path.parts or path.parts[0] != root_name
                or not (member.isfile() or member.isdir())
            ):
                raise BuildError(f"{source.name}: unsafe source archive member: {member.name!r}")
        tar.extractall(destination, members=members, filter="data")
    root = destination / root_name
    if not (root / "setup.py").is_file():
        raise BuildError(f"{source.name}: source archive is missing setup.py")
    return root


def _patch_setup(source: Source, root: Path) -> None:
    setup = root / "setup.py"
    content = setup.read_text(encoding="utf-8")
    old_exec = "exec(compile(f.read(), version_file, 'exec'))"
    old_return = "return locals()['__version__']"
    if content.count(old_exec) != 1 or content.count(old_return) != 1:
        raise BuildError(f"{source.name}: setup.py version lookup changed; patch must be reviewed")
    content = content.replace(
        old_exec, "_ver_ns = {}\n        exec(compile(f.read(), version_file, 'exec'), _ver_ns)",
    ).replace(old_return, "return _ver_ns['__version__']")
    if source.name == "basicsr":
        build_deps = "setup_requires=['cython', 'numpy', 'torch'],"
        if content.count(build_deps) != 1:
            raise BuildError("basicsr: setup.py build requirements changed; patch must be reviewed")
        # Native extensions are disabled for these portable wheels. These
        # expensive build requirements are unnecessary; install_requires is
        # deliberately preserved for the subsequent normal pip install.
        content = content.replace(build_deps, "setup_requires=[],")
    setup.write_text(content, encoding="utf-8")


def _validate_wheel(source: Source, wheel: Path) -> None:
    try:
        with zipfile.ZipFile(wheel) as archive:
            metadata = archive.read(f"{source.name}-{source.version}.dist-info/METADATA").decode("utf-8")
            if f"Name: {source.name}" not in metadata.splitlines() or f"Version: {source.version}" not in metadata.splitlines():
                raise BuildError(f"{source.name}: built wheel metadata does not match the pinned release")
            if archive.testzip() is not None:
                raise BuildError(f"{source.name}: built wheel is corrupt")
    except (OSError, KeyError, UnicodeError, zipfile.BadZipFile) as exc:
        raise BuildError(f"{source.name}: invalid built wheel: {exc}") from exc


def build_wheels(output_dir: Path) -> list[Path]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if wheels := _cached_wheels(output_dir):
        print(f">> Reusing verified Real-ESRGAN compatibility wheels in {output_dir}", flush=True)
        return wheels

    # Build completely in a private directory. Publish complete wheels and
    # then their digest manifest atomically so interrupted builds are retried.
    with tempfile.TemporaryDirectory(prefix=".build-", dir=output_dir) as temporary:
        work = Path(temporary)
        roots = []
        for source in SOURCES:
            archive = work / f"{source.name}.tar.gz"
            _download(source, archive)
            root = _extract(source, archive, work)
            _patch_setup(source, root)
            roots.append(root)
        wheel_dir = work / "wheels"
        wheel_dir.mkdir()
        print(">> Building portable Real-ESRGAN compatibility wheels", flush=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheel_dir), *map(str, roots)],
            check=True,
            env=dict(os.environ, BASICSR_EXT="False"),
        )
        built = []
        for source in SOURCES:
            wheel = wheel_dir / _wheel_name(source)
            _validate_wheel(source, wheel)
            built.append(wheel)
        manifest = {"build": _fingerprint(), "wheels": {wheel.name: _digest(wheel) for wheel in built}}
        manifest_path = work / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        for wheel in built:
            os.replace(wheel, output_dir / wheel.name)
        os.replace(manifest_path, output_dir / MANIFEST_NAME)
    return [output_dir / _wheel_name(source) for source in SOURCES]


def install_wheels(wheels: list[Path], *, user: bool = False, break_system_packages: bool = False) -> None:
    command = [sys.executable, "-m", "pip", "install", "--no-deps"]
    if user:
        command.append("--user")
    if break_system_packages:
        help_result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--help"],
            check=True, capture_output=True, text=True,
        )
        if "--break-system-packages" in help_result.stdout:
            command.append("--break-system-packages")
    print(">> Installing Real-ESRGAN compatibility wheels into the selected Python", flush=True)
    subprocess.run([*command, *map(str, wheels)], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    parser.add_argument("--install", action="store_true", help="Install the three wheels without dependencies")
    parser.add_argument("--user", action="store_true", help="Forward --user to the wheel installation")
    parser.add_argument("--break-system-packages", action="store_true", help="Forward the flag when supported by pip")
    args = parser.parse_args(argv)
    if (args.user or args.break_system_packages) and not args.install:
        parser.error("installation flags require --install")
    try:
        wheels = build_wheels(args.output_dir)
        if args.install:
            install_wheels(wheels, user=args.user, break_system_packages=args.break_system_packages)
    except (BuildError, OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print(f"Real-ESRGAN compatibility preparation failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
