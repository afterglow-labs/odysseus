"""Execute both download click paths and inspect the actual submitted payload."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def submitted_payload(path, state, selection, failure=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the download destination regression")
    cookbook = (ROOT / "static/js/cookbook.js").read_text()
    download = (ROOT / "static/js/cookbookDownload.js").read_text()
    functions = download.split("// ── Command builder: download ──", 1)[1].split(
        "// ── Panel rendering helpers ──", 1)[0]
    profiles = cookbook.split("function _isLocalEntry(s)", 1)[1].split(
        "export function _selectedServer()", 1)[0]
    script = (
        f"const _envState = {json.dumps(state)};\n"
        f"const selection = {json.dumps(selection)};\n"
        f"const failure = {json.dumps(failure)};\n"
        "let captured; let registered = 0; const toasts = [];\n"
        "const document = {getElementById: () => ({value: selection})};\n"
        "const uiModule = {showToast: (...args) => {toasts.push(args);}};\n"
        "const _getPort = () => ''; const _getPlatform = () => 'linux';\n"
        "const _isWindows = () => false; const _syncEnvFromPanel = () => {};\n"
        "const _loadTasks = () => []; const _addTask = () => {registered++;};\n"
        "const _renderRunningTab = () => {};\n"
        "const _retryDownload = (name, payload) => {captured = payload;};\n"
        "const fetch = async (url, options) => {captured = JSON.parse(options.body);\n"
        " if (failure) return {ok: false, status: failure.status, json: async () => {if (failure.json_error) throw Error('Not JSON'); return failure.body;}};\n"
        " return {ok: true, json: async () => ({ok: true, session_id: 'test'})};};\n"
        "function _isLocalEntry(s)" + profiles + functions
        + "\nconst window = {cookbookModule: {_serverKey}};\n"
    )
    if path == "model":
        script += download.split("// ── Model download (dedicated endpoint, tmux-backed) ──", 1)[1].split(
            "// ── Init ──", 1)[0]
        script += "\n_runModelDownload({}, {name: 'author/model-GGUF', quant: 'Q4_K_M'}, 'llamacpp')"
    else:
        script += "const dlInput = {value: 'author/model-GGUF:Q4_K_M'}; const dlGgufQuant = null;\n"
        script += "const _stripHfUrl = v => v; const _ggufQuantFromPath = () => 'Q4_K_M';\n"
        script += "function _splitRepoTag" + cookbook.split("function _splitRepoTag", 1)[1].split(
            "dlBtn.addEventListener('click', triggerDownload)", 1)[0]
        script += "\ntriggerDownload()"
    script += ".then(() => console.log(JSON.stringify(failure ? {captured, toasts, registered} : captured)));"
    result = subprocess.run([node, "-e", script.replace("export function", "function").replace(
        "export async function", "async function")], check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("failure,message", [
    ({"status": 409, "body": {"detail": "Windows drive E: is not mounted at /mnt/e. Mount the drive in WSL, then retry."}},
     "Windows drive E: is not mounted at /mnt/e. Mount the drive in WSL, then retry."),
    ({"status": 502, "json_error": True}, "HTTP 502"),
])
def test_download_error_keeps_server_mount_instructions_visible_without_tracking_a_job(failure, message):
    result = submitted_payload("model", {"servers": [], "localDownloadDir": "/mnt/e/Models"}, "local", failure)
    assert result["toasts"] == [["Download failed: " + message, 9000]]
    assert result["registered"] == 0


@pytest.mark.parametrize("path", ["model", "quick"])
@pytest.mark.parametrize("local_profile", [None, "", "local", "localhost"])
def test_local_click_uses_saved_destination_even_when_active_global_server_is_remote(path, local_profile):
    target = "/mnt/e/AI/huggingface/hub"
    state = {"remoteHost": "other.test", "servers": [],
             "localDownloadDir": target, "localHfCacheDir": "/private/cache/huggingface/hub"}
    if local_profile is not None:
        state["servers"] = [{"host": local_profile, "downloadDir": target}]
    payload = submitted_payload(path, state, "local")
    assert payload["local_dir"] == target
    assert "remote_host" not in payload
    assert payload["include"] == "*Q4_K_M*"


@pytest.mark.parametrize("path", ["model", "quick"])
def test_local_click_prefers_edited_profile_and_falls_back_to_named_models(path):
    state = {"servers": [{"host": "", "downloadDir": "/new/drive/hub"}],
             "localDownloadDir": "/old/drive/hub", "localHfCacheDir": "/private/hub"}
    assert submitted_payload(path, state, "local")["local_dir"] == "/new/drive/hub"
    state = {"servers": [], "localHfCacheDir": "/private/hub"}
    assert submitted_payload(path, state, "local")["local_dir"] == "~/models"


@pytest.mark.parametrize("path", ["model", "quick"])
@pytest.mark.parametrize("destination", ["", "/remote/chosen/hub"])
def test_remote_click_uses_selected_same_host_profile_without_local_drive(path, destination):
    state = {
        "servers": [
            {"host": "", "downloadDir": "/mnt/e/local/hub"},
            {"name": "first", "host": "remote.test", "downloadDir": "/wrong/hub", "port": "22"},
            {"name": "chosen", "host": "remote.test", "downloadDir": destination, "port": "2222"},
        ],
        "localDownloadDir": "/mnt/e/local/hub", "localHfCacheDir": "/private/hub",
    }
    payload = submitted_payload(path, state, "srv:chosen|remote.test|2222||")
    assert payload["remote_host"] == "remote.test"
    assert payload["remote_server_key"] == "srv:chosen|remote.test|2222||"
    assert payload["ssh_port"] == "2222"
    if destination:
        assert payload["local_dir"] == destination
    else:
        assert "local_dir" not in payload
