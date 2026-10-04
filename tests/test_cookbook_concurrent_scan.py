import asyncio
import json

import pytest
from starlette.requests import Request


@pytest.mark.asyncio
async def test_overlapping_cache_scans_keep_their_own_directories(tmp_path, monkeypatch):
    import routes.cookbook_routes as cookbook
    monkeypatch.setattr(cookbook, "require_admin", lambda request: None)
    monkeypatch.setattr(cookbook, "TMUX_LOG_DIR", tmp_path)
    monkeypatch.setattr(cookbook, "_cached_model_scan_script", lambda dirs: json.dumps(dirs))
    started = 0
    both_started = asyncio.Event()
    class Proc:
        returncode = 0
        async def communicate(self, data=None):
            nonlocal started
            started += 1
            if started == 2: both_started.set()
            await asyncio.wait_for(both_started.wait(), 2)
            assert data is not None
            model = {"repo_id": json.loads(data)[0], "size_bytes": 1, "nb_files": 1, "has_incomplete": False}
            return json.dumps([model]).encode(), b""
    async def execute(*args, **kwargs):
        assert args[-1] == "-"
        assert kwargs.get("stdin") == asyncio.subprocess.PIPE
        return Proc()
    monkeypatch.setattr(cookbook.asyncio, "create_subprocess_exec", execute)
    handler = next(r.endpoint for r in cookbook.setup_cookbook_routes().routes if r.path == "/api/model/cached")
    request = Request({"type": "http"})
    a,b = await asyncio.gather(handler(request, model_dir="/home/test/hub"), handler(request, model_dir="/mnt/c/test/hub"))
    assert a["models"][0]["repo_id"] == "/home/test/hub"
    assert b["models"][0]["repo_id"] == "/mnt/c/test/hub"
