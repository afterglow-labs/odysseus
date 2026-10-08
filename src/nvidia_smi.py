"""Read-only NVIDIA probes with a Windows telemetry fallback for broken WSL NVML."""
import asyncio
from pathlib import Path
import subprocess
import sys
import time


_native_retry_after = 0.0


def _windows_probe():
    if not sys.platform.startswith('linux'):
        return None
    try:
        if 'microsoft' not in Path('/proc/sys/kernel/osrelease').read_text().lower():
            return None
    except OSError:
        return None
    executable = Path('/mnt/c/Windows/System32/nvidia-smi.exe')
    return str(executable) if executable.is_file() else None


def _candidates(arguments):
    native = ['nvidia-smi', *arguments]
    # Host process IDs belong to Windows. Never expose them as WSL process IDs
    # or use the host executable for any mutation/reset command.
    telemetry = len(arguments) == 2 and arguments[0].startswith('--query-gpu=') and arguments[1] == '--format=csv,noheader,nounits'
    windows = _windows_probe() if telemetry else None
    if not windows:
        return [native]
    host = [windows, *arguments]
    return [host, native] if time.monotonic() < _native_retry_after else [native, host]


def _failed(command):
    global _native_retry_after
    if command[0] == 'nvidia-smi':
        # Stop repeatedly crashing the WSL binary on each UI telemetry poll.
        _native_retry_after = time.monotonic() + 60


def run_nvidia_smi(arguments, *, timeout=5):
    """Return a CompletedProcess, using live host GPU metrics if WSL fails."""
    commands = _candidates(arguments)
    for index, command in enumerate(commands):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            _failed(command)
            if index == len(commands) - 1:
                raise
            continue
        if result.returncode == 0:
            return result
        _failed(command)
        if index == len(commands) - 1:
            return result


async def run_nvidia_smi_async(arguments, *, timeout=8):
    """Async equivalent returning (stdout, error), without blocking requests."""
    error = 'nvidia-smi failed'
    for command in _candidates(arguments):
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                raise
            if process.returncode == 0:
                return stdout.decode('utf-8', errors='replace'), None
            error = stderr.decode('utf-8', errors='replace').strip()[:200] or 'nvidia-smi failed'
        except asyncio.TimeoutError:
            error = 'nvidia-smi timed out'
        except OSError as failure:
            error = str(failure)[:200]
        _failed(command)
    return None, error
