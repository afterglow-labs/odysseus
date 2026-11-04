"""Environment passed to processes that can handle agent-controlled input."""

import os
from collections.abc import Mapping


_AFTERGLOW_SERVER_SECRETS = frozenset({"AFTERGLOW_TOKEN", "AFTERGLOW_PASSWORD"})


def agent_subprocess_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Keep server-owned Afterglow credentials out of agent child processes.

    This limits routine environment inheritance. It is not a process isolation
    boundary: a same-user shell can still reach host files or process state.
    """
    if source is None:
        source = os.environ
    return {key: value for key, value in source.items()
            if key.upper() not in _AFTERGLOW_SERVER_SECRETS}
