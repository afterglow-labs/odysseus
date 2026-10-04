import os
import subprocess

import pytest

from routes.cookbook_helpers import _append_llama_cpp_capability_preflight_lines


@pytest.mark.parametrize("gpu,mtp,flags,expected", [
    (False, False, "-ngl 99", 78),
    (False, False, "-ngl 0", 0),
    (True, False, "-ngl 99 --spec-type draft-mtp", 78),
    (True, True, "-ngl 99 --spec-type draft-mtp --spec-draft-n-max 3", 0),
])
def test_requested_gpu_and_mtp_require_capable_runtime(tmp_path, gpu, mtp, flags, expected):
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\ncase \"$1\" in\n--help) echo '" + ("--spec-type draft-mtp" if mtp else "Python server") + "';;\n--list-devices) echo 'Available devices:'; echo '" + ("  CUDA0: Test GPU" if gpu else "  (none)") + "';;\nesac\n")
    binary.chmod(0o755)
    lines = []
    _append_llama_cpp_capability_preflight_lines(lines, "llama-server --model model.gguf " + flags)
    lines.append('exit "${ODYSSEUS_PREFLIGHT_EXIT:-0}"')
    result = subprocess.run(["bash", "-c", "\n".join(lines)], env={**os.environ, "PATH": str(tmp_path) + ":" + os.environ["PATH"]}, capture_output=True, text=True)
    assert result.returncode == expected, result.stdout + result.stderr
