import shutil
import subprocess
from pathlib import Path
import pytest


def test_explicit_reset_preserves_drafts_and_is_consumed_once():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    module = Path('static/js/videoMemoryReset.js').resolve().as_uri()
    script = f"import {{applyVideoMemoryReset}} from '{module}';\n" + """
import assert from 'node:assert/strict';
const entries = new Map(), storage = {getItem: k => entries.get(k), setItem: (k,v) => entries.set(k,v)};
const saved = {config: {prompt:'Keep my prompt', width:960, seed_hunter:{low_vram_chunks:3, output_fps:48}}, final_request:{request_id:'submitted', config:{seed_hunter:{low_vram_chunks:3}}}};
const reset = {revision:'request-1',low_vram_chunks:0};
assert.equal(applyVideoMemoryReset(saved,reset,'draft',storage),true);
assert.equal(saved.config.seed_hunter.low_vram_chunks,0);
assert.equal(saved.config.seed_hunter.output_fps,48);
assert.equal(saved.config.prompt,'Keep my prompt');
assert.equal(saved.config.width,960);
assert.equal(saved.final_request.config.seed_hunter.low_vram_chunks,3);
saved.config.seed_hunter.low_vram_chunks=5;
assert.equal(applyVideoMemoryReset(saved,reset,'draft',storage),false);
assert.equal(saved.config.seed_hunter.low_vram_chunks,5);
assert.equal(applyVideoMemoryReset(saved,null,'draft',storage),false);
"""
    result = subprocess.run([node, '--input-type=module'], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
