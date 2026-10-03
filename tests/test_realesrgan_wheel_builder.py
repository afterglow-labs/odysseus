"""The shared builder repairs verified sdists without changing the app Python."""

import ast
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace
import zipfile

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_realesrgan_wheels.py"
_SPEC = importlib.util.spec_from_file_location("realesrgan_wheel_builder", _SCRIPT)
builder = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = builder
_SPEC.loader.exec_module(builder)


def _setup_text(source):
    text = """def get_version():
    with open(version_file, 'r') as f:
        exec(compile(f.read(), version_file, 'exec'))
    return locals()['__version__']

setup(
    install_requires=['torch>=1.7', 'torchvision'],
"""
    if source.name == "basicsr":
        text += "    setup_requires=['cython', 'numpy', 'torch'],\n"
    return text + ")\n"


def _archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, value in entries:
            entry = tarfile.TarInfo(name)
            if isinstance(value, bytes):
                entry.size = len(value)
                archive.addfile(entry, io.BytesIO(value))
            else:
                entry.type = tarfile.SYMTYPE
                entry.linkname = value
                archive.addfile(entry)
    return stream.getvalue()


@pytest.fixture
def build_fixture(monkeypatch):
    archives = {}
    sources = []
    for source in builder.SOURCES:
        contents = _archive([(f"{source.name}-{source.version}/setup.py", _setup_text(source).encode())])
        source = source._replace(sha256=hashlib.sha256(contents).hexdigest())
        archives[source.url] = contents
        sources.append(source)
    monkeypatch.setattr(builder, "SOURCES", tuple(sources))
    fetches = []
    calls = []

    def urlopen(url, timeout):
        assert timeout == 30
        fetches.append(url)
        return io.BytesIO(archives[url])

    def run(command, *, check, env):
        calls.append(command)
        assert check is True
        assert command[:5] == [sys.executable, "-m", "pip", "wheel", "--no-deps"]
        assert env["BASICSR_EXT"] == "False"
        output_dir = Path(command[6])
        for source, root in zip(sources, command[7:], strict=True):
            text = (Path(root) / "setup.py").read_text()
            assert "return _ver_ns['__version__']" in text
            assert "install_requires=['torch>=1.7', 'torchvision']" in text
            assert "setup_requires=['cython', 'numpy', 'torch']" not in text
            wheel = output_dir / builder._wheel_name(source)
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    f"{source.name}-{source.version}.dist-info/METADATA",
                    f"Metadata-Version: 2.1\nName: {source.name}\nVersion: {source.version}\nRequires-Dist: torch>=1.7\n",
                )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(builder.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(builder.subprocess, "run", run)
    return SimpleNamespace(fetches=fetches, calls=calls, archives=archives, run=run)


def test_builds_all_patches_and_reuses_only_verified_cache(tmp_path, build_fixture):
    wheels = builder.build_wheels(tmp_path)
    assert len(wheels) == 3
    assert len(build_fixture.calls) == 1
    assert len(build_fixture.fetches) == 3
    assert builder.build_wheels(tmp_path) == wheels
    assert len(build_fixture.calls) == 1
    wheels[0].write_bytes(b"interrupted or modified wheel")
    builder.build_wheels(tmp_path)
    assert len(build_fixture.calls) == 2
    assert not list(tmp_path.glob(".build-*"))


@pytest.mark.parametrize("source", builder.SOURCES, ids=lambda source: source.name)
def test_patched_version_lookup_works_on_current_python(tmp_path, source):
    setup = tmp_path / "setup.py"
    setup.write_text(_setup_text(source))
    version = tmp_path / "version.py"
    version.write_text(f"__version__ = {source.version!r}\n")
    builder._patch_setup(source, tmp_path)
    # Execute the real patched function, excluding setup() and package imports.
    module = ast.parse(setup.read_text())
    module.body = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    namespace = {"version_file": str(version)}
    exec(compile(module, str(setup), "exec"), namespace)
    assert namespace["get_version"]() == source.version


def test_changed_source_patch_fails_loudly(tmp_path):
    (tmp_path / "setup.py").write_text("# A different upstream setup.py\n")
    with pytest.raises(builder.BuildError, match="patch must be reviewed"):
        builder._patch_setup(builder.SOURCES[0], tmp_path)


def test_bad_source_digest_never_reaches_build(tmp_path, build_fixture):
    build_fixture.archives[builder.SOURCES[0].url] = b"unexpected source"
    with pytest.raises(builder.BuildError, match="SHA256"):
        builder.build_wheels(tmp_path)
    assert not build_fixture.calls
    assert not list(tmp_path.iterdir())


def test_oversized_download_is_bounded(tmp_path, build_fixture, monkeypatch):
    monkeypatch.setattr(builder, "MAX_DOWNLOAD_BYTES", 2)
    with pytest.raises(builder.BuildError, match="download limit"):
        builder.build_wheels(tmp_path)
    assert not build_fixture.calls
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "entry",
    [
        ("../escape.py", b"bad"),
        ("/absolute.py", b"bad"),
        ("basicsr-1.4.2/../../escape.py", b"bad"),
        ("basicsr-1.4.2\\escape.py", b"bad"),
        ("other-root/setup.py", b"bad"),
        ("basicsr-1.4.2/link", "../../outside"),
    ],
)
def test_unsafe_archive_rejected_before_extraction(tmp_path, entry):
    archive = tmp_path / "source.tar.gz"
    archive.write_bytes(_archive([entry]))
    destination = tmp_path / "extracted"
    with pytest.raises(builder.BuildError, match="unsafe source archive"):
        builder._extract(builder.SOURCES[0], archive, destination)
    assert not destination.exists()


@pytest.mark.parametrize("limit", ["MAX_ARCHIVE_MEMBERS", "MAX_EXTRACTED_BYTES"])
def test_archive_expansion_is_bounded_before_extraction(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(builder, limit, 1)
    archive = tmp_path / "source.tar.gz"
    archive.write_bytes(_archive([
        ("basicsr-1.4.2/setup.py", b"setup text"),
        ("basicsr-1.4.2/other.py", b"other text"),
    ]))
    destination = tmp_path / "extracted"
    with pytest.raises(builder.BuildError, match="extraction limits"):
        builder._extract(builder.SOURCES[0], archive, destination)
    assert not destination.exists()


def test_failed_build_publishes_no_manifest_or_wheels(tmp_path, build_fixture, monkeypatch):
    def fail(command, **kwargs):
        build_fixture.run(command, **kwargs)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(builder.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        builder.build_wheels(tmp_path)
    assert not list(tmp_path.iterdir())


def test_cache_revision_change_requires_rebuild(tmp_path, build_fixture, monkeypatch):
    builder.build_wheels(tmp_path)
    monkeypatch.setattr(builder, "PATCH_REVISION", builder.PATCH_REVISION + 1)
    builder.build_wheels(tmp_path)
    assert len(build_fixture.calls) == 2
    assert json.loads((tmp_path / builder.MANIFEST_NAME).read_text())["build"]["patch_revision"] == builder.PATCH_REVISION


@pytest.mark.parametrize("supports_break_flag", [True, False])
def test_install_preserves_interpreter_and_user_flags_without_dependencies(monkeypatch, tmp_path, supports_break_flag):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["check"] is True
        return SimpleNamespace(stdout="--break-system-packages" if supports_break_flag else "old pip help")

    monkeypatch.setattr(builder.subprocess, "run", run)
    wheels = [tmp_path / builder._wheel_name(source) for source in builder.SOURCES]
    builder.install_wheels(wheels, user=True, break_system_packages=True)
    assert calls[0] == [sys.executable, "-m", "pip", "install", "--help"]
    command = calls[1]
    assert command[:6] == [sys.executable, "-m", "pip", "install", "--no-deps", "--user"]
    assert ("--break-system-packages" in command) is supports_break_flag
    assert command[-3:] == [str(wheel) for wheel in wheels]


def test_default_cli_build_does_not_install(tmp_path, build_fixture):
    assert builder.main(["--output-dir", str(tmp_path)]) == 0
    assert len(build_fixture.calls) == 1


def test_cli_failure_is_nonzero_and_actionable(tmp_path, build_fixture, capsys):
    build_fixture.archives[builder.SOURCES[0].url] = b"unexpected source"
    assert builder.main(["--output-dir", str(tmp_path), "--install"]) == 1
    assert "source SHA256 does not match" in capsys.readouterr().err
    assert not build_fixture.calls
