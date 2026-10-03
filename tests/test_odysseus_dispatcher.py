from tests.helpers.cli_loader import load_script


def test_is_runnable_subcommand_requires_executable_file(tmp_path):
    cli = load_script("odysseus")
    sub = tmp_path / "odysseus-demo"
    sub.write_text("#!/bin/sh\n")
    sub.chmod(0o644)

    assert cli._is_runnable_subcommand(sub) is False

    sub.chmod(0o755)
    assert cli._is_runnable_subcommand(sub) is True


def test_help_runs_under_project_python(tmp_path, monkeypatch):
    cli = load_script("odysseus")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    sub = scripts / "odysseus-demo"
    sub.write_text("#!/usr/bin/env python3\n")
    sub.chmod(0o755)
    monkeypatch.setattr(cli, "SCRIPTS_DIR", scripts)
    python = tmp_path / "venv" / ("Scripts/python.exe" if cli.os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    called = []
    monkeypatch.setattr(cli.subprocess, "call", lambda args: called.append(args) or 0)

    assert cli.main(["help", "demo"]) == 0
    assert called == [[str(python), str(sub), "--help"]]


def test_command_uses_same_python_as_help(tmp_path, monkeypatch):
    cli = load_script("odysseus")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    sub = scripts / "odysseus-demo"
    sub.write_text("#!/usr/bin/env python3\n")
    sub.chmod(0o755)
    monkeypatch.setattr(cli, "SCRIPTS_DIR", scripts)
    called = []
    monkeypatch.setattr(cli.os, "execv", lambda executable, args: called.append((executable, args)))

    assert cli.main(["demo", "list"]) == 0
    expected = cli._subcommand_python()
    assert called == [(expected, [expected, str(sub), "list"])]
