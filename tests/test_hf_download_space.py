"""Disk failures must stop before transfer and must never enter the retry loop."""

import os
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from routes.cookbook_output import hf_download_attempt_lines
from src import hf_download_space as space


def stub_preview(monkeypatch, files, free, *, error=None):
    seen = {}

    def snapshot_download(*, repo_id, cache_dir, allow_patterns, dry_run):
        seen.update(repo_id=repo_id, cache_dir=cache_dir,
                    allow_patterns=allow_patterns, dry_run=dry_run)
        if error:
            raise error
        return [SimpleNamespace(**entry) for entry in files]

    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        SimpleNamespace(snapshot_download=snapshot_download))
    monkeypatch.setattr(space.shutil, "disk_usage", lambda path: SimpleNamespace(free=free))
    return seen


def test_large_selected_file_stops_before_downloading(monkeypatch, tmp_path, capsys):
    seen = stub_preview(monkeypatch, [{"will_download": True, "file_size": 16_796_010_000}],
                        4_250_830_000)
    destination = tmp_path / "chosen-drive" / "hub"
    assert space.check_download_space("org/model", "*Q4_K_M*", str(destination)) == 28
    assert seen == {"repo_id": "org/model", "cache_dir": str(destination),
                    "allow_patterns": ["*Q4_K_M*"], "dry_run": True}
    output = capsys.readouterr().out
    assert "16.80 GB" in output and "4.25 GB" in output
    assert str(destination) in output and "DOWNLOAD_NO_SPACE" in output
    assert not destination.exists()


def test_cached_files_are_excluded_and_missing_sizes_summed(monkeypatch, tmp_path):
    stub_preview(monkeypatch, [{"will_download": False, "file_size": 90_000_000_000},
                              {"will_download": True, "file_size": 500_000_000},
                              {"will_download": True, "file_size": 600_000_000}],
                 1_000_000_000 + space.DOWNLOAD_RESERVE)
    assert space.check_download_space("org/model", "", str(tmp_path)) == 28


def test_completed_cached_files_do_not_require_more_space(monkeypatch, tmp_path):
    stub_preview(monkeypatch, [{"will_download": False, "file_size": 90_000_000_000}], 0)
    assert space.check_download_space("org/model", "", str(tmp_path)) == 0


def test_chosen_volume_not_process_working_directory(monkeypatch, tmp_path):
    stub_preview(monkeypatch, [{"will_download": True, "file_size": 15_000_000_000}],
                 13_000_000_000_000)
    volume = tmp_path / "E"
    volume.mkdir()
    checked = []
    monkeypatch.setattr(space.shutil, "disk_usage", lambda path: (
        checked.append(path) or SimpleNamespace(free=13_000_000_000_000)))
    assert space.check_download_space("org/model", "*.gguf", str(volume / "AI/hub")) == 0
    assert checked == [volume]


def test_sparse_abandoned_partial_does_not_count_as_cached(monkeypatch, tmp_path):
    partial = tmp_path / "anything.deadbeef.incomplete"
    with partial.open("wb") as stream:
        stream.truncate(16_000_000_000)
    stub_preview(monkeypatch, [{"will_download": True, "file_size": 16_800_000_000}],
                 4_000_000_000)
    assert space.check_download_space("org/model", "", str(tmp_path)) == 28
    assert partial.stat().st_size == 16_000_000_000


def test_metadata_failure_is_not_misreported_as_disk_failure(monkeypatch, tmp_path):
    stub_preview(monkeypatch, [], 0, error=RuntimeError("Hub connection unavailable"))
    with pytest.raises(RuntimeError, match="Hub connection unavailable"):
        space.check_download_space("org/model", "", str(tmp_path))


def test_unknown_sizes_are_reported_without_fabricating_disk_failure(monkeypatch, tmp_path, capsys):
    stub_preview(monkeypatch, [{"will_download": True, "file_size": None}], 4_000_000_000)
    assert space.check_download_space("org/model", "", str(tmp_path)) == 0
    assert "file sizes are unknown" in capsys.readouterr().out


def run_retry_fixture(tmp_path, *, required, free, download_script, old_hub=False):
    stub = tmp_path / "huggingface_hub.py"
    signature = "repo_id, cache_dir, allow_patterns" if old_hub else "repo_id, cache_dir, allow_patterns, dry_run"
    stub.write_text(
        "from types import SimpleNamespace\nimport shutil\n"
        f"shutil.disk_usage = lambda path: SimpleNamespace(free={free})\n"
        f"def snapshot_download(*, {signature}):\n"
        f" return [SimpleNamespace(will_download=True, file_size={required})]\n"
    )
    count = tmp_path / "download-count"
    command = "bash -c " + shlex.quote(
        "printf 'attempt\\n' >> " + shlex.quote(str(count)) + "\n" + download_script)
    lines = ["_attempt=0; _ec=0", "while [ $_attempt -lt 3 ]; do",
             "  _attempt=$((_attempt+1))"]
    lines.extend(hf_download_attempt_lines(shlex.quote(sys.executable), command,
                                          "org/model", "*Q4*"))
    lines.extend(["  if [ $_ec -eq 0 ]; then break; fi", "done",
                  'echo "attempts=$_attempt code=$_ec"', "exit $_ec"])
    result = subprocess.run(["bash", "-c", "\n".join(lines)],
                            env={**os.environ, "PYTHONPATH": str(tmp_path),
                                 "HF_HUB_CACHE": str(tmp_path / "hub"),
                                 "HF_TOKEN": "secret-must-not-be-printed"},
                            text=True, capture_output=True, timeout=20)
    assert "secret-must-not-be-printed" not in result.stdout + result.stderr
    return result, count.read_text().splitlines() if count.exists() else []


def test_shell_preflight_blocks_transfer_and_retry(tmp_path):
    result, attempts = run_retry_fixture(tmp_path, required=16_800_000_000,
                                        free=4_000_000_000, download_script="exit 0")
    assert result.returncode == 28
    assert not attempts
    assert "attempts=1 code=28" in result.stdout


@pytest.mark.parametrize("error", ["No space left on device", "Not enough free disk space to download the file.",
                                   "Disk quota exceeded"])
def test_runtime_disk_failure_is_not_retried_even_without_preflight(tmp_path, error):
    result, attempts = run_retry_fixture(tmp_path, required=1, free=100_000_000_000,
                                        old_hub=True,
                                        download_script="echo " + shlex.quote(error) + " >&2; exit 1")
    assert result.returncode == 28
    assert len(attempts) == 1
    assert error in result.stdout
    assert "DOWNLOAD_NO_SPACE" in result.stdout


def test_runtime_network_failure_remains_retryable(tmp_path):
    result, attempts = run_retry_fixture(tmp_path, required=1000, free=100_000_000_000,
                                        download_script="echo 'Connection reset by peer' >&2; exit 7")
    assert result.returncode == 7
    assert len(attempts) == 3
    assert "Connection reset by peer" in result.stdout
    assert "DOWNLOAD_NO_SPACE" not in result.stdout


def test_success_exit_survives_tee(tmp_path):
    result, attempts = run_retry_fixture(tmp_path, required=1000, free=100_000_000_000,
                                        download_script="echo 'Download complete'; exit 0")
    assert result.returncode == 0
    assert len(attempts) == 1
    assert "Download complete" in result.stdout
