"""Linux GPU process discovery when WSL's NVML process list is incomplete."""
import os
from pathlib import Path


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
