"""Select the private SGLang runtime without changing other engine defaults."""
import re
import shlex
import sys

from src.sglang_runtime import is_sglang_install_cmd, is_sglang_launch_cmd, managed_sglang_venv


# Replace only the executable, preserving shell quoting/expansion in arguments.
_DEFAULT_PYTHON = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|[^\s]+)\s+)*"
    r"(?P<python>python3|python)(?=\s)"
)


def local_sglang_command(cmd, *, remote_host=None, env_prefix=None, platform=None):
    """Return (command, managed) for implicit local Linux SGLang commands.

    An explicit interpreter, environment or SSH target always wins. The runtime
    is selected even before it exists, so a missing installation cannot fall
    back to the main app Python or global site-packages.
    """
    if remote_host or env_prefix or not (platform or sys.platform).startswith("linux"):
        return cmd, False
    if not (is_sglang_install_cmd(cmd) or is_sglang_launch_cmd(cmd)):
        return cmd, False
    match = _DEFAULT_PYTHON.match(cmd)
    if not match:
        return cmd, False
    start, end = match.span("python")
    python = shlex.quote(str(managed_sglang_venv() / "bin/python"))
    return cmd[:start] + python + cmd[end:], True


def append_sglang_prepare(lines, *, app_python):
    """Prepare the isolated environment inside the existing logged pip task."""
    script = managed_sglang_venv().parents[1] / "scripts/setup_sglang_runtime.py"
    lines.extend([
        f"{shlex.quote(app_python)} {shlex.quote(str(script))} --prepare-only",
        "_odysseus_sglang_setup_exit=$?",
        'if [ "$_odysseus_sglang_setup_exit" -ne 0 ]; then',
        '  ODYSSEUS_PREFLIGHT_EXIT="$_odysseus_sglang_setup_exit"',
        "fi",
    ])
