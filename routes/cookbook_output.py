"""Pure helpers for shaping cookbook task output for the status response.

Kept dependency-free (no FastAPI / SQLAlchemy imports) so the behavior can be
unit-tested without standing up the whole app.
"""

import re
import shlex
from pathlib import Path

from src import hf_cache, hf_download_space

_HF_CACHE_SOURCE = Path(hf_cache.__file__).read_text(encoding="utf-8")

HF_DOWNLOAD_SPACE_PROBE = (
    _HF_CACHE_SOURCE + "\n"
    + Path(hf_download_space.__file__).read_text(encoding="utf-8")
    + "\nimport sys\nsys.exit(check_download_space(sys.argv[1], "
      "sys.argv[2] if len(sys.argv) > 2 else '', effective_hf_cache()))\n"
)


def hf_download_attempt_lines(python_command, download_command, repo_id, pattern):
    """One bash retry-loop attempt; known disk failures terminate the loop.

    Keep the actual download exit status across tee. Its temporary transcript
    catches runtime ENOSPC too (unknown sizes, older Hub, concurrent writers).
    Network failures retain their original output and remain retryable.
    """
    probe = (python_command + " -c " + shlex.quote(HF_DOWNLOAD_SPACE_PROBE)
             + " " + shlex.quote(repo_id) + " " + shlex.quote(pattern or ""))
    return [
        "  " + probe,
        "  _ec=$?",
        "  if [ $_ec -eq 0 ]; then",
        "    _odysseus_download_log=$(mktemp) || { _ec=28; break; }",
        "    " + download_command + ' 2>&1 | tee "$_odysseus_download_log"',
        "    _ec=${PIPESTATUS[0]}",
        "    if [ $_ec -ne 0 ] && grep -Eiq "
        "'No space left on device|Not enough free disk space|There is not enough space on the disk|Disk quota exceeded|\\[Errno (28|122)\\]' "
        '"$_odysseus_download_log"; then',
        '      echo "DOWNLOAD_NO_SPACE: Download stopped because the destination ran out of space. Choose another download folder; this failure will not be retried."',
        "      _ec=28",
        "    fi",
        '    rm -f "$_odysseus_download_log"',
        "  fi",
        "  if [ $_ec -eq 28 ]; then break; fi",
    ]

_FETCHING_ZERO_FILES_RE = re.compile(r"Fetching\s+0\s+files", re.IGNORECASE)

# `hf download` can exit 0 and print a nonexistent snapshot path when its
# include pattern matches nothing. Recent noninteractive CLIs also suppress
# the "Fetching 0 files" progress line, so validate the materialized files
# before the runner prints DOWNLOAD_OK. This is entirely offline.
HF_CACHE_MATCHING_FILES_PROBE = _HF_CACHE_SOURCE + "\n" + r'''
import fnmatch, ntpath, os, sys
repo = sys.argv[1]
# Legacy PowerShell may omit an empty final native-command argument.
pattern = sys.argv[2] if len(sys.argv) > 2 else ''
base = os.path.join(effective_hf_cache(), 'models--' + repo.replace('/', '--'))
snapshots = os.path.join(base, 'snapshots')
ref = os.path.join(base, 'refs', 'main')
roots = []
if os.path.isfile(ref):
    with open(ref, encoding='utf-8') as f:
        revision = f.read().strip()
    if revision and revision == ntpath.basename(revision) and revision not in ('.', '..'):
        roots = [os.path.join(snapshots, revision)]
elif os.path.isdir(snapshots):
    roots = [os.path.join(snapshots, name) for name in os.listdir(snapshots)]
found = False
for root in roots:
    for directory, _, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, root).replace(os.sep, '/')
            if name.endswith('.incomplete') or (pattern and not fnmatch.fnmatchcase(relative, pattern)):
                continue
            try:
                if os.path.getsize(path) > 0:
                    found = True
                    break
            except OSError:
                pass
        if found:
            break
    if found:
        break
if not found:
    print('DOWNLOAD_EMPTY: No matching files were downloaded. Check the filename/quant pattern for this repository.')
sys.exit(0 if found else 1)
'''


def download_has_zero_files(full_snapshot: str) -> bool:
    return "DOWNLOAD_EMPTY:" in full_snapshot or bool(_FETCHING_ZERO_FILES_RE.search(full_snapshot))

# Probe scripts for the dead-session download check, run as
# `python3 -c <PROBE> <repo_id> <cache_root>` (locally or over SSH).
# cache_root is the task's custom download dir, '' for the default HF cache.
# It has to be passed explicitly: the download runner exports
# HF_HOME and the hub itself explicitly, so the selected path (home or hub)
# must be preserved for these probes, and
# the probe process's own environment knows nothing about it.
HF_CACHE_COMPLETE_PROBE = (
    _HF_CACHE_SOURCE + "\n"
    "import os,sys;"
    "repo=sys.argv[1];"
    "root=os.path.expanduser(sys.argv[2]) if len(sys.argv)>2 and sys.argv[2] else '';"
    "base=download_cache_paths(root)[1];"
    "d=os.path.join(base,'models--'+repo.replace('/','--'));"
    "snap=os.path.join(d,'snapshots');"
    "ok=os.path.isdir(snap) and any(os.path.isdir(os.path.join(snap,x)) and os.listdir(os.path.join(snap,x)) for x in os.listdir(snap));"
    "inc=False;"
    "blobs=os.path.join(d,'blobs');"
    "inc=os.path.isdir(blobs) and any(x.endswith('.incomplete') for x in os.listdir(blobs));"
    "sys.exit(0 if ok and not inc else 1)"
)

HF_CACHE_INCOMPLETE_PROBE = (
    _HF_CACHE_SOURCE + "\n"
    "import os,sys;"
    "repo=sys.argv[1];"
    "root=os.path.expanduser(sys.argv[2]) if len(sys.argv)>2 and sys.argv[2] else '';"
    "base=download_cache_paths(root)[1];"
    "d=os.path.join(base,'models--'+repo.replace('/','--'));"
    "blobs=os.path.join(d,'blobs');"
    "inc=os.path.isdir(blobs) and any(x.endswith('.incomplete') for x in os.listdir(blobs));"
    "sys.exit(0 if inc else 1)"
)


def classify_dead_download(full_snapshot: str):
    """Resolve a dead download session's status from its runner markers.

    The runner prints DOWNLOAD_OK only after exiting 0 (and DOWNLOAD_FAILED
    otherwise), so the markers stay trustworthy after the tmux pane is gone.
    Returns (status, zero_files), or None when the snapshot carries no marker
    and the caller has to fall back to the cache probe. Same precedence as
    the live-session branch: DOWNLOAD_OK wins, except a "Fetching 0 files"
    run is an error (nothing matched the include/quant pattern).
    """
    if not full_snapshot:
        return None
    if "DOWNLOAD_EMPTY:" in full_snapshot:
        return ("error", True)
    if "DOWNLOAD_OK" in full_snapshot:
        if download_has_zero_files(full_snapshot):
            return ("error", True)
        return ("completed", False)
    if "DOWNLOAD_FAILED" in full_snapshot:
        return ("error", False)
    return None


def error_aware_output_tail(full_snapshot: str, status: str) -> str:
    """Return the trailing slice of a task log for the status response.

    Failed tasks return the last 50 lines so the "Copy last 50 lines" action
    surfaces the actual error context (stack traces, build output). Running and
    other non-error tasks keep the cheaper 12-line tail to limit the payload on
    the 10s polling interval.
    """
    if not full_snapshot:
        return ""
    tail_lines = 50 if status == "error" else 12
    return "\n".join(full_snapshot.splitlines()[-tail_lines:])
