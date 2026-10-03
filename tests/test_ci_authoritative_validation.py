"""Regression coverage for authoritative Python CI validation.

Workflow source checks are intentional: runner selection and blocking gates
cannot be exercised by a local unit test.
"""

import re
from pathlib import Path


_WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
)


def _indented_block(text: str, heading: str, indent: int) -> str:
    pattern = re.compile(
        rf"(?ms)^{' ' * indent}{re.escape(heading)}:\n"
        rf"(?P<body>(?:(?:{' ' * (indent + 2)}.*|\s*)\n)*)"
    )
    match = pattern.search(text)
    assert match is not None, f"missing {heading!r} block"
    return match.group(0)


def test_ci_runs_on_integrated_dev_pushes():
    workflow = _WORKFLOW.read_text()
    push = _indented_block(workflow, "push", 2)

    assert re.search(r"(?m)^    branches:\s*\[main,\s*dev\]\s*$", push)
    assert "paths-ignore:" not in push


def test_python_tests_are_authoritative():
    workflow = _WORKFLOW.read_text()
    python_tests = _indented_block(workflow, "python-tests", 2)

    assert "python -m pytest -q" in python_tests
    assert "continue-on-error:" not in python_tests


def test_workflows_use_the_repository_python_version():
    for path in _WORKFLOW.parent.glob("*.yml"):
        text = path.read_text()
        for setup in re.findall(r"uses: actions/setup-python[^\n]*\n(?:[ ]{8,}.+\n)*", text):
            assert "python-version-file: .python-version" in setup, path.name
            assert not re.search(r"\bpython-version:", setup), path.name


def test_native_and_container_startup_checks_are_blocking():
    workflow = _WORKFLOW.read_text()
    native = _indented_block(workflow, "native-runtime", 2)
    container = _indented_block(workflow, "container-runtime", 2)
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in native
    assert "--require-hashes -r requirements.lock" in native
    assert "python scripts/smoke_runtime.py" in native
    assert "bash docker/smoke-image.sh" in container
    for job in (native, container):
        assert "continue-on-error:" not in job


def test_docker_stages_share_the_canonical_python_default():
    repo = _WORKFLOW.parents[2]
    expected = (repo / ".python-version").read_text().strip()
    dockerfile = (repo / "Dockerfile").read_text()
    assert f"ARG PYTHON_VERSION={expected}\n" in dockerfile
    bases = re.findall(r"(?m)^FROM (\S+)", dockerfile)
    assert bases and all(base == "python:${PYTHON_VERSION}-slim" for base in bases)
