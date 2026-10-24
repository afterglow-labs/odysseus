"""The deployed worker is launched as a script, without project-package imports."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('mode', ['interactive', 'scout', 'final', 'automatic'])
def test_private_python_script_imports_reach_scout_and_continue_chosen_latent(tmp_path, mode):
    project = Path(__file__).parents[1]
    if not (project / 'runtimes/minimax-h3/seed-scout/nodes.py').is_file():
        pytest.skip('Pinned SeedScout runtime is not installed')
    probe = project / 'tests/fixtures/h3_scout_worker_entrypoint.py'
    environment = {key: value for key, value in os.environ.items() if key not in {'PYTHONPATH', 'PYTHONHOME'}}
    result = subprocess.run([sys.executable, '-I', str(probe), str(project), str(tmp_path), mode],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-12000:] + result.stdout[-2000:]
    chosen = 43 if mode == 'interactive' else 42
    assert f'completed chosen seed {chosen}; no CUDA initialization' in result.stdout
