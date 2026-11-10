"""The quant picker must select upstream paths with case-sensitive Hub globs."""

import fnmatch
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("files,quant,expected", [
    (["baronllm-llama3.1-v1-q6_k.gguf"], "Q6_K", ["baronllm-llama3.1-v1-q6_k.gguf"]),
    (["weights/model-Q6_K.GGUF"], "Q6_K", ["weights/model-Q6_K.GGUF"]),
    (["model-q6_k-00001-of-00002.gguf", "model-q6_k-00002-of-00002.gguf", "model-Q4_K_M.gguf"],
     "Q6_K", ["model-q6_k-00001-of-00002.gguf", "model-q6_k-00002-of-00002.gguf"]),
    (["q6_k/model-00001-of-00002.gguf", "q6_k/model-00002-of-00002.gguf", "q4_k_m/model.gguf"],
     "Q6_K", ["q6_k/model-00001-of-00002.gguf", "q6_k/model-00002-of-00002.gguf"]),
    (["model-Q4_K_M.gguf", "model-Q4_K_S.gguf", "mmproj.gguf"], "Q4_K_M", ["model-Q4_K_M.gguf"]),
    (["a-q6_k.gguf", "b-Q6_K.gguf", "c-Q4_K_M.gguf"], "Q6_K", None),
    (["model-Q4_K_M.gguf"], "Q6_K", None),
])
def test_picker_selects_all_and_only_files_for_quant(files, quant, expected):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required")
    source = (ROOT / "static/js/cookbook.js").read_text()
    functions = "function _ggufQuantFromPath" + source.split("function _ggufQuantFromPath", 1)[1].split(
        "function _hideGgufPicker", 1)[0]
    result = subprocess.run([node, "-e", functions +
        f"console.log(JSON.stringify(_ggufIncludeForQuant({json.dumps(files)}, {json.dumps(quant)})));"],
        capture_output=True, text=True, check=True, timeout=10)
    pattern = json.loads(result.stdout)
    if expected is None:
        assert not pattern, "Missing or ambiguous selections must not include unrelated files"
        return
    assert pattern, "The selected quant must have a usable file pattern"
    assert [name for name in files if fnmatch.fnmatchcase(name, pattern)] == expected
    if len(expected) == 1:
        assert pattern == expected[0], "A single-file quant should retain its exact upstream filename"
