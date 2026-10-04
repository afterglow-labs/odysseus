"""The desktop entry point must launch the app's venv, never PATH Python."""

import json
from pathlib import Path
import sys
import venv

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop launcher")
def test_desktop_child_uses_private_environment_and_existing_profile(tmp_path, monkeypatch):
    from src.windows_desktop import launch_desktop

    repo = tmp_path / "Odysseus with spaces"
    runtime = repo / "venv"
    venv.EnvBuilder(with_pip=False).create(runtime)
    profile = tmp_path / "existing profile"
    profile.mkdir()
    (profile / "keep.txt").write_text("restored profile", encoding="utf-8")
    (repo / "launcher.py").write_text(
        "import json, os, site, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from src.constants import DATA_DIR, AUTH_FILE\n"
        "Path(os.environ['DATA_DIR'], 'child.json').write_text(json.dumps({\n"
        "'executable': sys.executable, 'prefix': sys.prefix, 'args': sys.argv[1:],\n"
        "'user_site': site.ENABLE_USER_SITE, 'path': sys.path,\n"
        "'app_data': DATA_DIR, 'auth_file': AUTH_FILE,\n"
        "'pip_user': os.environ.get('PIP_USER'),\n"
        "'pip_require_venv': os.environ.get('PIP_REQUIRE_VIRTUALENV')}))\n",
        encoding="utf-8",
    )
    shared = tmp_path / "shared packages"
    shared.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(shared))
    monkeypatch.setenv("PIP_USER", "1")
    child = launch_desktop(repo, data_dir=profile)
    assert child.wait(timeout=15) == 0
    result = json.loads((profile / "child.json").read_text())
    assert Path(result["executable"]) == runtime / "Scripts/python.exe"
    assert Path(result["prefix"]) == runtime
    assert result["args"] == ["--desktop"]
    assert result["user_site"] is False
    assert str(shared) not in result["path"]
    assert result["pip_user"] == "0"
    assert result["pip_require_venv"] == "true"
    assert Path(result["app_data"]) == profile
    assert Path(result["auth_file"]) == profile / "auth.json"
    assert (profile / "keep.txt").read_text() == "restored profile"


def test_missing_app_environment_never_falls_back_to_global_python(tmp_path):
    from src.windows_desktop import launch_desktop

    with pytest.raises(RuntimeError, match="environment"):
        launch_desktop(tmp_path, data_dir=tmp_path / "profile")
