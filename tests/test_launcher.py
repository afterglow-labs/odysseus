# tests/test_launcher.py
import sys
import os
from pathlib import Path
from unittest import mock
import pytest

from launcher import (
    app_browser_candidates, ensure_standard_streams, create_tray_image,
    on_open_browser, on_exit, open_browser, open_app_window,
)


def test_frozen_multiprocessing_bootstrap_precedes_gui_and_app_imports():
    source = Path("launcher.py").read_text(encoding="utf-8")

    freeze = source.index("multiprocessing.freeze_support()")
    splash = source.index("if getattr(sys, 'frozen', False):")
    app_import = source.index("from app import app")
    assert freeze < splash < app_import


def test_windowed_streams_support_subprocess_stderr(monkeypatch):
    import subprocess

    for name in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(sys, name, None)
    ensure_standard_streams()
    streams = (sys.stdin, sys.stdout, sys.stderr)
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import sys; sys.stderr.write('worker ready')"],
            stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, timeout=10,
        )
        assert result.returncode == 0
        assert all(stream.fileno() >= 0 for stream in streams)
    finally:
        for stream in streams:
            stream.close()


def test_existing_standard_streams_are_preserved():
    streams = (sys.stdin, sys.stdout, sys.stderr)
    ensure_standard_streams()
    assert (sys.stdin, sys.stdout, sys.stderr) == streams


def test_create_tray_image():
    try:
        from PIL import Image
        img = create_tray_image()
        assert isinstance(img, Image.Image)
        assert img.size == (64, 64)
    except ImportError:
        pytest.skip("Pillow/PIL not installed in test environment")


def test_on_open_browser():
    with mock.patch("launcher.open_app_window") as mock_open:
        icon_mock = mock.Mock()
        item_mock = mock.Mock()
        url = "http://127.0.0.1:7000"
        on_open_browser(icon_mock, item_mock, url)
        mock_open.assert_called_once_with(url)


def test_on_exit():
    with mock.patch("os._exit") as mock_exit:
        icon_mock = mock.Mock()
        item_mock = mock.Mock()
        on_exit(icon_mock, item_mock)
        icon_mock.stop.assert_called_once()
        mock_exit.assert_called_once_with(0)


def test_open_browser():
    with mock.patch("launcher.open_app_window") as mock_open, \
         mock.patch("time.sleep") as mock_sleep:

        # Test when splash_root is None
        with mock.patch("launcher.splash_root", None):
            open_browser("http://127.0.0.1:7000")
            mock_open.assert_called_once_with("http://127.0.0.1:7000")
            mock_sleep.assert_called_once_with(3.5)

    with mock.patch("launcher.open_app_window") as mock_open, \
         mock.patch("time.sleep") as mock_sleep:
        # Test when splash_root is present and gets destroyed
        mock_splash = mock.Mock()
        with mock.patch("launcher.splash_root", mock_splash):
            open_browser("http://127.0.0.1:7000")
            mock_splash.after.assert_called_once()


def test_finds_windows_browser_in_install_directory(tmp_path, monkeypatch):
    browser = tmp_path / "Microsoft/Edge/Application/msedge.exe"
    browser.parent.mkdir(parents=True)
    browser.touch()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("launcher.shutil.which", lambda name: None)
    for name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path))
    assert app_browser_candidates() == [str(browser)]


def test_opens_standalone_app_instead_of_default_browser_tab():
    with mock.patch("launcher.app_browser_candidates", return_value=["edge.exe"]), \
         mock.patch("launcher.subprocess.Popen") as launch, \
         mock.patch("launcher.webbrowser.open") as default_browser:
        open_app_window("http://127.0.0.1:7000")
        launch.assert_called_once_with(
            ["edge.exe", "--app=http://127.0.0.1:7000", "--new-window"],
        )
        default_browser.assert_not_called()


def test_tries_next_app_browser_when_first_cannot_launch():
    with mock.patch("launcher.app_browser_candidates", return_value=["edge.exe", "chrome.exe"]), \
         mock.patch("launcher.subprocess.Popen", side_effect=[OSError("unavailable"), mock.Mock()]) as launch, \
         mock.patch("launcher.webbrowser.open") as default_browser:
        open_app_window("http://127.0.0.1:7000")
        assert launch.call_args.args[0][0] == "chrome.exe"
        default_browser.assert_not_called()


def test_falls_back_when_no_app_browser_is_installed():
    with mock.patch("launcher.app_browser_candidates", return_value=[]), \
         mock.patch("launcher.webbrowser.open") as default_browser:
        open_app_window("http://127.0.0.1:7000")
        default_browser.assert_called_once_with("http://127.0.0.1:7000")
