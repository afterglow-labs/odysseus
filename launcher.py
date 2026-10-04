# launcher.py
"""Dedicated entrypoint for the standalone Windows portable launcher.

Handles:
- Immediate GUI splash screen creation using tkinter.
- Providing valid standard streams in windowed GUI mode.
- Spawning system tray icon via pystray and Pillow (lazy-loaded).
- Opening the interface in a standalone browser app window.
- Launching the FastAPI server (importing and running app.py).
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

# PyInstaller multiprocessing children re-enter this executable with a private
# bootstrap argument. Consume it before splash/UI or application imports so a
# spawn-based worker does not relaunch the full desktop application.
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    from src.python_runtime import require_supported_python
    require_supported_python()
    if len(sys.argv) > 1 and sys.argv[1] == "--mcp-server":
        from src.frozen_mcp import run_builtin_server
        if len(sys.argv) != 3:
            raise SystemExit("Usage: Odysseus-worker.exe --mcp-server SERVER_ID")
        run_builtin_server(sys.argv[2])
        raise SystemExit(0)


def ensure_standard_streams():
    """Give windowed builds real handles usable by logging and subprocesses."""
    for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, mode, encoding="utf-8"))


ensure_standard_streams()


splash_root = None

# If running from a frozen PyInstaller bundle, launch the splash screen IMMEDIATELY
if getattr(sys, 'frozen', False):
    import tkinter as tk

    def show_splash_instantly():
        global splash_root
        try:
            splash_root = tk.Tk()
            splash_root.title("Odysseus")
            splash_root.overrideredirect(True)
            splash_root.configure(bg="#1a1c23")

            # Accented borders
            splash_root.config(highlightbackground="#e06c75", highlightcolor="#e06c75", highlightthickness=1)

            w, h = 360, 160
            ws = splash_root.winfo_screenwidth()
            hs = splash_root.winfo_screenheight()
            x = (ws - w) // 2
            y = (hs - h) // 2
            splash_root.geometry(f"{w}x{h}+{x}+{y}")

            tk.Label(splash_root, text="⛵ Odysseus", font=("Segoe UI", 22, "bold"), bg="#1a1c23", fg="#e06c75").pack(pady=(22, 2))
            tk.Label(splash_root, text="Launching background services...", font=("Segoe UI", 10), bg="#1a1c23", fg="#d1d4e0").pack(pady=2)
            tk.Label(splash_root, text="Please wait, this will take a few seconds.", font=("Segoe UI", 8, "italic"), bg="#1a1c23", fg="#5c6370").pack(pady=(12, 0))

            splash_root.attributes("-topmost", True)
            splash_root.mainloop()
        except Exception:
            pass

    # Launch the GUI splash screen immediately on a background thread
    threading.Thread(target=show_splash_instantly, daemon=True).start()


def create_tray_image():
    # Generate a beautiful 64x64 icon matching Odysseus brand red accent (#e06c75)
    from PIL import Image, ImageDraw
    image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    dc = ImageDraw.Draw(image)
    accent_red = (224, 108, 117, 255)
    light_red = (224, 108, 117, 150)

    # Draw premium sailing boat
    dc.polygon([(32, 10), (32, 45), (12, 45)], fill=accent_red)
    dc.polygon([(32, 18), (32, 45), (48, 45)], fill=light_red)
    dc.polygon([(8, 48), (56, 48), (44, 56), (20, 56)], fill=accent_red)
    return image


def app_browser_candidates():
    """Find Windows browsers that support the same app mode as our Mac launcher."""
    if sys.platform != "win32":
        return []
    candidates = []
    for executable, relative in (
        ("msedge.exe", "Microsoft/Edge/Application/msedge.exe"),
        ("chrome.exe", "Google/Chrome/Application/chrome.exe"),
        ("brave.exe", "BraveSoftware/Brave-Browser/Application/brave.exe"),
    ):
        on_path = shutil.which(executable)
        if on_path:
            candidates.append(on_path)
        for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            root = os.environ.get(variable)
            if root:
                candidate = os.path.join(root, *relative.split("/"))
                if os.path.isfile(candidate):
                    candidates.append(candidate)
    return list(dict.fromkeys(candidates))


def open_app_window(url):
    for browser in app_browser_candidates():
        try:
            subprocess.Popen([browser, f"--app={url}", "--new-window"])
            return
        except OSError:
            continue
    webbrowser.open(url)


def on_open_browser(icon, item, url):
    open_app_window(url)


def on_exit(icon, item):
    icon.stop()
    os._exit(0)


def setup_system_tray(url):
    try:
        import pystray
        icon_img = create_tray_image()
        menu = (
            pystray.MenuItem('Open Odysseus', lambda icon, item: on_open_browser(icon, item, url), default=True),
            pystray.MenuItem('Exit', on_exit)
        )
        tray_icon = pystray.Icon(
            "Odysseus",
            icon_img,
            "Odysseus",
            menu
        )
        tray_icon.run()
    except Exception:
        pass


def open_browser(url):
    # Allow uvicorn and app lifecycles to complete warmups
    time.sleep(3.5)

    # Safely close the splash screen
    try:
        global splash_root
        if splash_root:
            splash_root.after(0, splash_root.destroy)
    except Exception:
        pass

    open_app_window(url)


if __name__ == "__main__":
    bind_host = os.getenv("APP_BIND", "127.0.0.1")
    bind_port = int(os.getenv("APP_PORT", "7000"))
    url_host = "127.0.0.1" if bind_host in {"0.0.0.0", "::"} else bind_host
    url = f"http://{url_host}:{bind_port}"
    desktop = "--desktop" in sys.argv or getattr(sys, "frozen", False)

    if "--desktop" in sys.argv and sys.prefix == sys.base_prefix:
        raise SystemExit("Desktop mode requires Odysseus's own virtual environment.")
    if desktop:
        # Reopening the desktop shortcut should reuse the existing service.
        import json
        from urllib.request import urlopen
        try:
            with urlopen(url + "/api/health", timeout=2) as response:
                running = json.load(response).get("status") == "healthy"
        except Exception:
            running = False
        if running:
            open_app_window(url)
            raise SystemExit(0)

    import uvicorn
    from app import app

    if desktop:
        # Start browser manager thread
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()
        # Start system tray manager thread
        threading.Thread(target=setup_system_tray, args=(url,), daemon=True).start()

    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")
