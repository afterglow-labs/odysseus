"""Stable GPU selection and process discovery when WSL's NVML is incomplete."""
import os
import re
from pathlib import Path


GPU_UUID_PATTERN = r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"


def resolve_nvidia_selection(selection, inventory):
    """Map nvidia-smi picker indexes to stable CUDA UUIDs, retaining order.

    CUDA device ordinals can differ from nvidia-smi indexes, especially in
    WSL. ``inventory`` is CSV from --query-gpu=index,name,uuid.
    """
    by_index = {}
    for line in (inventory or '').splitlines():
        parts = [part.strip() for part in line.split(',')]
        if len(parts) >= 3 and parts[0].isdigit() and re.fullmatch(GPU_UUID_PATTERN, parts[-1]):
            by_index[parts[0]] = parts[-1]
    if not by_index:
        raise ValueError('NVIDIA GPU identities could not be verified; refresh the GPU probe before launching.')
    available = set(by_index.values())
    resolved = []
    for device in str(selection).split(','):
        value = by_index.get(device) if device.isdigit() else device
        if value not in available:
            raise ValueError(f'Selected GPU {device} is not available on the selected server; refresh the GPU probe.')
        if value not in resolved:
            resolved.append(value)
    return ','.join(resolved)


def replace_cuda_visibility(command, selection):
    """Keep inline assignments consistent with the runner's resolved UUIDs."""
    return re.sub(
        r"(?<![\w])((?:\$env:)?CUDA_VISIBLE_DEVICES\s*=\s*)(?:'[^']*'|\"[^\"]*\"|[^\s;&|]+)",
        lambda match: match[1] + "'" + selection + "'",
        command or '',
    )


def process_start_time(pid, proc_root=Path('/proc')):
    try:
        # comm can contain spaces and parentheses; starttime is field 22.
        return (proc_root / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def wsl_gpu_processes(proc_root=Path('/proc'), exclude_pid=None):
    """Return Linux /dev/dxg holders, never Windows host PIDs from nvidia-smi."""
    excluded = set()
    pid = os.getpid() if exclude_pid is None else exclude_pid
    while pid > 0 and pid not in excluded:
        excluded.add(pid)
        try:
            status = (proc_root / str(pid) / 'status').read_text()
            pid = int(next(line.split()[1] for line in status.splitlines() if line.startswith('PPid:')))
        except (OSError, ValueError, StopIteration):
            break
    result = []
    for directory in proc_root.glob('[0-9]*'):
        pid = int(directory.name)
        if pid < 100 or pid in excluded:
            continue
        try:
            name = (directory / 'comm').read_text().strip()
            if name in {'nvidia-smi', 'weston', 'Xwayland'}:
                continue
            holds_gpu = False
            for fd in (directory / 'fd').iterdir():
                try:
                    if os.readlink(fd) == '/dev/dxg':
                        holds_gpu = True
                        break
                except OSError:
                    continue
            if holds_gpu:
                result.append({'pid': pid, 'name': name[:80], 'used_mb': None,
                               'start_time': process_start_time(pid, proc_root), 'source': 'wsl-device'})
        except OSError:
            continue
    return result
