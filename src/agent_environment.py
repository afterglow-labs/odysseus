"""Environment passed to processes that can handle agent-controlled input."""

import os
import sys
from collections.abc import Mapping


_AFTERGLOW_SERVER_SECRETS = frozenset({"AFTERGLOW_TOKEN", "AFTERGLOW_PASSWORD"})
_PR_SET_DUMPABLE = 4
_PR_GET_DUMPABLE = 3


def protect_server_process_secrets(source: Mapping[str, str] | None = None) -> bool:
    """Deny same-user reads of a Linux server's sensitive process state.

    Agent children still receive a scrubbed environment. Linux also needs the
    parent to be non-dumpable so they cannot read its original environment via
    /proc/<pid>/environ or attach a debugger. This does not hide readable files.
    """
    if source is None:
        source = os.environ
    if not any(value for key, value in source.items()
               if key.upper() in _AFTERGLOW_SERVER_SECRETS):
        return False
    if sys.platform != "linux":
        return False

    import ctypes

    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                      ctypes.c_ulong, ctypes.c_ulong)
    prctl.restype = ctypes.c_int
    if prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot protect Afterglow server credentials")
    if prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise RuntimeError("Afterglow server credential process protection did not take effect")
    return True


def agent_subprocess_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Keep server-owned Afterglow credentials out of agent child processes.

    This limits routine environment inheritance. It is not a process isolation
    boundary: a same-user shell can still reach host files or process state.
    """
    if source is None:
        source = os.environ
    return {key: value for key, value in source.items()
            if key.upper() not in _AFTERGLOW_SERVER_SECRETS}
