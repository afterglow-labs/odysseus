"""Scout history and bulk editing receive different server receipt shapes."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_scout_filter_includes_bulk_queue_receipts_and_excludes_hunter_jobs():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required')
    module = (Path(__file__).parents[1] / 'static/js/h3SeedScout.js').as_uri()
    result = subprocess.run([node, '--input-type=module'], input=f"import {{isSeedScoutJob}} from '{module}';\n" + """
import assert from 'node:assert/strict';
const bulk=[
 {id:'scout',recipe:'seed_hunter',config:{recipe:'seed_hunter',seed_hunter:{stage:'scout'}}},
 {id:'hunter',recipe:'seed_hunter',config:{recipe:'seed_hunter',seed_hunter:{stage:'preview'}}},
 {id:'refine',recipe:'seed_hunter',config:{recipe:'seed_hunter',seed_hunter:{stage:'final'}}},
 {id:'h3',config:{mode:'ref2va'}}
];
assert.deepEqual(bulk.filter(isSeedScoutJob).map(job=>job.id),['scout']);
assert.equal(isSeedScoutJob({id:'running',seed_hunter:{stage:'scout'}}),true);
assert.equal(isSeedScoutJob({id:'waiting',scout:{status:'waiting_for_selection'}}),true);
assert.equal(isSeedScoutJob({id:'h3',scout:null}),false);
assert.equal(isSeedScoutJob(null),false);
""", capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
