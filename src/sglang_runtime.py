"""SGLang's isolated runtime and safe Cookbook command handling.

The app can run a newer Python than SGLang's published native stack. Keep
the compatibility check in the selected interpreter, including SSH targets;
never infer it from the Python that is serving the Cookbook request.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


SGLANG_REQUIREMENT = "sglang>=0.5.21"
SGLANG_PYTHON_MIN = (3, 10)
SGLANG_PYTHON_MAX = (3, 13)


def managed_sglang_venv() -> Path:
    """Return the app-owned runtime location without creating or installing it."""
    return Path(__file__).resolve().parents[1] / ".venvs" / "sglang"


@dataclass(frozen=True)
class _Token:
    value: str
    start: int
    end: int


def _tokens(cmd: str) -> list[_Token]:
    """Tokenize one simple shell command while retaining untouched source text.

    In particular, rebuilding the entire command with shlex.join would turn
    an expanding "$HOME/venv/bin/python" into a literal dollar-sign path.
    Compound shell commands are outside this helper's supported grammar.
    """
    result: list[_Token] = []
    index = 0
    try:
        while index < len(cmd):
            if cmd[index] in "\r\n":
                return []
            if cmd[index].isspace():
                index += 1
                continue
            start = index
            quote = ""
            while index < len(cmd):
                char = cmd[index]
                if quote != "'" and (char == "`" or cmd[index:index + 2] == "$("):
                    return []
                if char == "\\" and quote != "'":
                    index += 2
                    continue
                if quote:
                    if char == quote:
                        quote = ""
                elif char in "\"'":
                    quote = char
                elif char.isspace():
                    if char in "\r\n":
                        return []
                    break
                elif char in ";&|<>`":
                    return []
                index += 1
            if quote:
                return []
            values = shlex.split(cmd[start:index])
            if len(values) != 1:
                return []
            result.append(_Token(values[0], start, index))
    except ValueError:
        return []
    return result


def _command_start(tokens: list[_Token]) -> int:
    index = 0
    while index < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index].value):
        index += 1
    return index


def _python_command(tokens: list[_Token], start: int) -> bool:
    return start < len(tokens) and bool(re.fullmatch(
        r"python(?:[23](?:\.\d+)?)?(?:\.exe)?",
        tokens[start].value.replace("\\", "/").rsplit("/", 1)[-1],
    ))


def _pip_args_start(tokens: list[_Token]) -> int | None:
    start = _command_start(tokens)
    if not _python_command(tokens, start):
        return None
    index = start + 1
    while index < len(tokens) and tokens[index].value in {"-u", "-s", "-S", "-E", "-I", "-B"}:
        index += 1
    if [token.value for token in tokens[index:index + 3]] == ["-m", "pip", "install"]:
        return index + 3
    return None


_PIP_VALUE_OPTIONS = frozenset({
    "-r", "--requirement", "-c", "--constraint", "-i", "--index-url",
    "--extra-index-url", "-f", "--find-links", "--trusted-host", "--target", "-t",
    "--root", "--prefix", "--platform", "--python-version", "--implementation",
    "--abi", "--proxy", "--retries", "--timeout", "--cert", "--client-cert",
    "--log", "--cache-dir", "--only-binary", "--no-binary", "--progress-bar",
    "--config-settings", "-C", "--use-feature", "--use-deprecated",
    "--keyring-provider", "--src", "--report", "--build-constraint",
})


def _sglang_requirements(tokens: list[_Token]) -> list[tuple[int, Requirement]]:
    index = _pip_args_start(tokens)
    if index is None:
        return []
    found = []
    options = True
    while index < len(tokens):
        value = tokens[index].value
        if options and value == "--":
            options = False
            index += 1
            continue
        if options and value.startswith("-"):
            index += 2 if value in _PIP_VALUE_OPTIONS else 1
            continue
        try:
            requirement = Requirement(value)
        except InvalidRequirement:
            index += 1
            continue
        # A spaced direct reference is not our managed PyPI recipe.
        if index + 1 < len(tokens) and tokens[index + 1].value == "@":
            index += 3
            continue
        if canonicalize_name(requirement.name) == "sglang" and not requirement.url:
            found.append((index, requirement))
        index += 1
    return found


def is_sglang_install_cmd(cmd: str) -> bool:
    return bool(_sglang_requirements(_tokens(cmd or "")))


def is_sglang_launch_cmd(cmd: str) -> bool:
    tokens = _tokens(cmd or "")
    start = _command_start(tokens)
    if start >= len(tokens):
        return False
    binary = tokens[start].value.replace("\\", "/").rsplit("/", 1)[-1]
    if binary == "sglang":
        return start + 1 < len(tokens) and tokens[start + 1].value == "serve"
    if not _python_command(tokens, start):
        return False
    index = start + 1
    while index < len(tokens) and tokens[index].value in {"-u", "-s", "-S", "-E", "-I", "-B"}:
        index += 1
    return [token.value for token in tokens[index:index + 2]] == ["-m", "sglang.launch_server"]


def normalize_sglang_install_cmd(cmd: str) -> str:
    """Modernize only actual SGLang requirements, keeping explicit constraints.

    Current CUDA releases use prerelease dependencies. Requiring a SGLang
    wheel prevents pip backtracking into obsolete source releases whose
    build dependencies can no longer be installed.
    """
    tokens = _tokens(cmd or "")
    requirements = _sglang_requirements(tokens)
    if not requirements:
        return cmd
    changes: list[tuple[int, int, str]] = []
    for index, requirement in requirements:
        token = tokens[index]
        # Keep all other extras, pins, ranges, and environment markers.
        match = re.match(r"^[A-Za-z0-9_.-]+(?:\[([^]]*)\])?", token.value)
        assert match is not None
        extras = sorted(extra for extra in requirement.extras if canonicalize_name(extra) != "all")
        replacement = "sglang" + ("[" + ",".join(extras) + "]" if extras else "")
        if not requirement.specifier:
            replacement += SGLANG_REQUIREMENT[len("sglang"):]
        replacement += token.value[match.end():]
        changes.append((token.start, token.end, shlex.quote(replacement)))
    normalized = cmd
    for start, end, replacement in reversed(changes):
        normalized = normalized[:start] + replacement + normalized[end:]
    values = [token.value for token in tokens]
    flags = []
    if "--pre" not in values:
        flags.append("--pre")
    if "--only-binary=sglang" not in values and "--only-binary=:all:" not in values:
        flags.append("--only-binary=sglang")
    if flags:
        # Insert before a possible `--` requirements delimiter.
        delimiter = next((token for token in tokens if token.value == "--"), None)
        if delimiter:
            offset = sum(len(replacement) - (end - start) for start, end, replacement in changes if start < delimiter.start)
            position = delimiter.start + offset
            normalized = normalized[:position] + " ".join(flags) + " " + normalized[position:]
        else:
            normalized = normalized.rstrip() + " " + " ".join(flags)
    return normalized


def sglang_python_compatibility_error(version: tuple[int, int], executable: str = "") -> str:
    if SGLANG_PYTHON_MIN <= tuple(version[:2]) <= SGLANG_PYTHON_MAX:
        return ""
    selected = ".".join(map(str, version))
    return (
        f"SGLang requires Python 3.10–3.13 for the supported wheel installation; "
        f"selected Python is {selected}" + (f" ({executable})" if executable else "") + ". "
        "Use Odysseus's isolated SGLang Python 3.12 environment (.venvs/sglang), "
        "or select a Python 3.12 venv for this server, then retry. "
        "The application's Python and video runtime do not need to be changed."
    )


def sglang_python_preflight_code() -> str:
    """Standalone selected-Python check, requiring no Odysseus imports remotely."""
    message = "ERROR: " + sglang_python_compatibility_error((0, 0), "{executable}").replace("0.0", "{version}", 1)
    return (
        "import sys\n"
        f"if not {SGLANG_PYTHON_MIN!r} <= sys.version_info[:2] <= {SGLANG_PYTHON_MAX!r}:\n"
        f"    print({message!r}.format(version='.'.join(map(str, sys.version_info[:3])), executable=sys.executable), file=sys.stderr)\n"
        "    sys.exit(78)\n"
        "print('[odysseus] SGLang Python: ' + sys.executable + ' (' + '.'.join(map(str, sys.version_info[:3])) + ')')\n"
    )
