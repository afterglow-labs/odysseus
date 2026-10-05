"""Verified shutdown of one POSIX Cookbook job, also runnable over SSH.

Capture identities before closing tmux: engines can ignore SIGHUP or create
their own process groups. Never kill by model name, port, or GPU membership.
This module uses only the standard library so the same code runs remotely.
"""

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

MARKER = "ODYSSEUS_COOKBOOK_SESSION"


def _snapshot():
    """pid -> (parent pid, start identity); omit zombies and other users."""
    result = {}
    if sys.platform.startswith("linux"):
        for entry in Path("/proc").glob("[0-9]*"):
            try:
                if entry.stat().st_uid != os.geteuid():
                    continue
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] not in {"Z", "X"}:
                    result[int(entry.name)] = (int(fields[1]), fields[19])
            except (FileNotFoundError, ProcessLookupError):
                pass  # process exited during enumeration
        return result
    output = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,uid=,lstart=,stat="],
        capture_output=True, text=True, check=True, timeout=5,
    ).stdout
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 9 and int(fields[2]) == os.geteuid() and not fields[8].startswith("Z"):
            result[int(fields[0])] = (int(fields[1]), " ".join(fields[3:8]))
    return result


def _marked(session_id, processes):
    """Recover detached children even if their original terminal disappeared."""
    marker = (MARKER + "=" + session_id).encode()
    result = set()
    if sys.platform.startswith("linux"):
        for pid in processes:
            try:
                if marker in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                    result.add(pid)
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                pass
    else:
        output = subprocess.run(["ps", "eww", "-axo", "pid=,command="],
                                capture_output=True, text=True, check=True, timeout=5).stdout
        pattern = re.compile(r"(?:^|\s)" + re.escape(marker.decode()) + r"(?:\s|$)")
        for line in output.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and int(fields[0]) in processes and pattern.search(fields[1]):
                result.add(int(fields[0]))
    return result


def _tmux(binary, *args):
    result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=5)
    if result.returncode:
        if any(part in result.stderr.lower() for part in
               ("can't find session", "no server running", "no sessions", "error connecting")):
            # A missing socket is expected after the last session exits;
            # permission failures or a refused connection are not proof of exit.
            if "error connecting" not in result.stderr.lower() or "no such file" in result.stderr.lower():
                return None
        raise RuntimeError("Could not inspect the job's tmux session: " + result.stderr.strip()[:300])
    return result.stdout


def stop_job(session_id, directory, grace_seconds=2.0, term_seconds=3.0, kill_seconds=2.0):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise ValueError("Invalid Cookbook job identity")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # Serialize duplicate taps/retries, including separate SSH invocations.
    with (directory / (session_id + ".stop.lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _stop_locked(session_id, directory, grace_seconds, term_seconds, kill_seconds)


def _stop_locked(session_id, directory, grace_seconds, term_seconds, kill_seconds):
    binary = shutil.which("tmux")
    if not binary:
        raise RuntimeError("tmux is unavailable on the job's server")
    target = "=" + session_id  # exact match, never tmux's prefix matching
    receipt = directory / (session_id + ".processes.json")
    existed = receipt.exists()
    owned = json.loads(receipt.read_text()) if existed else {}
    owned = {int(pid): identity for pid, identity in owned.items()}
    processes = _snapshot()
    excluded = {1}
    ancestor = os.getpid()
    while ancestor and ancestor not in excluded:
        excluded.add(ancestor)
        ancestor = processes.get(ancestor, (0, ""))[0]

    def remember():
        temporary = receipt.with_suffix(".tmp")
        temporary.write_text(json.dumps(owned))
        temporary.chmod(0o600)
        temporary.replace(receipt)

    def collect():
        current = _snapshot()
        # list-panes -s can silently fall back to the current session when
        # its target disappears, even with '=name'. Filter explicit names
        # from all panes so a neighboring job can never become our target.
        panes = _tmux(binary, "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}")
        roots = set()
        for line in (panes or "").splitlines():
            name, _, pid = line.partition("\t")
            if name == session_id and pid.isdigit():
                roots.add(int(pid))
        session_alive = bool(roots)
        roots.update(pid for pid, identity in owned.items() if current.get(pid, (0, None))[1] == identity)
        roots.update(_marked(session_id, current))
        roots.difference_update(excluded)
        while True:
            descendants = {pid for pid, (parent, _) in current.items() if parent in roots and pid not in excluded}
            if descendants <= roots:
                break
            roots.update(descendants)
        found = {pid: current[pid][1] for pid in roots if pid in current}
        if any(owned.get(pid) != identity for pid, identity in found.items()):
            owned.update(found)
            remember()  # preserve ownership even after parent/tmux exits
        return found, session_alive

    active, session_alive = collect()
    if not active and not session_alive and not existed:
        return {"ok": False, "status": "unknown", "error":
                "The job's terminal is already gone, but no process record exists to verify shutdown. "
                "Check the server's GPU processes before relaunching this older job."}
    remember()
    for sig, timeout in ((signal.SIGINT, grace_seconds), (signal.SIGTERM, term_seconds), (signal.SIGKILL, kill_seconds)):
        if not active:
            break
        # Signal each owned PID, including children in separate process groups.
        # Recheck start identity immediately before every signal (PID reuse).
        for pid, identity in active.items():
            if _snapshot().get(pid, (0, None))[1] == identity:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + timeout
        while True:
            active, session_alive = collect()
            if not active or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    # Close an idle terminal only after capturing/stopping its children.
    if not active and session_alive:
        _tmux(binary, "kill-session", "-t", target)
    active, session_alive = collect()
    if active or session_alive:
        return {"ok": False, "status": "running", "remaining_pids": sorted(active),
                "error": "Shutdown could not be verified; this job still has running processes or an open terminal."}
    return {"ok": True, "status": "stopped", "session_id": session_id, "remaining_pids": []}


if __name__ == "__main__":
    try:
        result = stop_job(sys.argv[1], "/tmp/odysseus-tmux")
    except Exception as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result))
