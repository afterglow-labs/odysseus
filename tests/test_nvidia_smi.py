"""Broken WSL NVML can use live Windows telemetry without mixing PID spaces."""
import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import nvidia_smi as probes
from src import h3_video


WINDOWS = '/mnt/c/Windows/System32/nvidia-smi.exe'
QUERY = ['--query-gpu=uuid,name,compute_cap', '--format=csv,noheader,nounits']
ROWS = ('GPU-a9435034-aa17-d151-1a9d-fb1060eb0609, NVIDIA GeForce RTX 5090, 12.0\r\n'
        'GPU-ae65ab63-8a09-72d8-0d20-c01ae72d00f1, NVIDIA GeForce RTX 4090, 8.9\r\n')


@pytest.fixture(autouse=True)
def clean_probe_state(monkeypatch):
    monkeypatch.setattr(probes, '_native_retry_after', 0)
    monkeypatch.setattr(probes, '_windows_probe', lambda: WINDOWS)


def test_wsl_crash_uses_live_host_metrics_and_cools_down_native_probe(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=-11 if command[0] == 'nvidia-smi' else 0, stdout=ROWS)
    monkeypatch.setattr(probes.subprocess, 'run', run)
    result = probes.run_nvidia_smi(QUERY, timeout=2)
    assert result.stdout == ROWS and result.returncode == 0
    assert [call[0] for call in calls] == ['nvidia-smi', WINDOWS]
    calls.clear()
    probes.run_nvidia_smi(QUERY)
    assert [call[0] for call in calls] == [WINDOWS]


@pytest.mark.parametrize('failure', [FileNotFoundError('absent'), subprocess.TimeoutExpired('nvidia-smi', 2)])
def test_missing_or_hung_native_probe_can_fall_back(monkeypatch, failure):
    def run(command, **kwargs):
        if command[0] == 'nvidia-smi':
            raise failure
        return SimpleNamespace(returncode=0, stdout=ROWS)
    monkeypatch.setattr(probes.subprocess, 'run', run)
    assert probes.run_nvidia_smi(QUERY).stdout == ROWS


def test_successful_native_probe_remains_preferred(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=ROWS)
    monkeypatch.setattr(probes.subprocess, 'run', run)
    probes.run_nvidia_smi(QUERY)
    assert [call[0] for call in calls] == ['nvidia-smi']


@pytest.mark.parametrize('arguments', [
    ['--query-compute-apps=pid,gpu_uuid,process_name,used_memory', '--format=csv,noheader,nounits'],
    ['--gpu-reset'], ['--query-gpu=uuid', '--gpu-reset'],
])
def test_windows_fallback_never_exposes_host_pids_or_runs_mutations(arguments):
    assert probes._candidates(arguments) == [['nvidia-smi', *arguments]]


@pytest.mark.asyncio
async def test_async_cookbook_probe_recovers_from_same_native_crash(monkeypatch):
    calls = []
    async def execute(*command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=-11 if command[0] == 'nvidia-smi' else 0,
                               communicate=AsyncMock(return_value=(ROWS.encode(), b'')))
    monkeypatch.setattr(probes.asyncio, 'create_subprocess_exec', execute)
    output, error = await probes.run_nvidia_smi_async(QUERY)
    assert output == ROWS and error is None
    assert [call[0] for call in calls] == ['nvidia-smi', WINDOWS]


def test_non_wsl_keeps_native_only(monkeypatch):
    monkeypatch.setattr(probes, '_windows_probe', lambda: None)
    assert probes._candidates(QUERY) == [['nvidia-smi', *QUERY]]


def test_h3_fallback_preserves_uuid_and_nvfp4_architecture_eligibility(monkeypatch):
    def run(command, **kwargs):
        return SimpleNamespace(returncode=-11 if command[0] == 'nvidia-smi' else 0, stdout=ROWS)
    monkeypatch.setattr(probes.subprocess, 'run', run)
    inventory = h3_video.gpu_inventory()
    assert [gpu['name'] for gpu in inventory] == ['NVIDIA GeForce RTX 5090', 'NVIDIA GeForce RTX 4090']
    assert [gpu['nvfp4'] for gpu in inventory] == [True, False]
    assert inventory[1]['id'] == 'GPU-ae65ab63-8a09-72d8-0d20-c01ae72d00f1'
