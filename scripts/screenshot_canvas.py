"""Take the one screenshot the headless script can't: the dashboard beside an agent session in VS Code.

    uv run python scripts/screenshot_canvas.py --repo microsoft/PyRIT --pr 3774

Windows only, and a real session: it starts a second VS Code on a throwaway profile (so the window is clean and
the same every time), opens a new agent session there with a prompt that asks the agent to open the canvas and
place the PR on the map, and then needs two things from you: press Enter to send the prompt (VS Code never sends
one from a link), and press Enter here once the agent has asked its question. It then sizes the window, captures
it, and writes docs/img/canvas.webp. The model turn costs what a turn costs.

The throwaway profile has to be signed in to GitHub once (the Accounts menu in that window); the sign-in stays
in the profile under %TEMP%, so later runs don't ask. Needs the live dashboard running with the repo built, and
the canvas installed (`readthrough install canvas`).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab

from readthrough.config import load_settings
from readthrough.serve import repo_url, running_server
from readthrough.system import canvas_state

OUT = Path(__file__).resolve().parents[1] / "docs" / "img" / "canvas.webp"
PROFILE = Path(tempfile.gettempdir()) / "readthrough-canvas-shot"
WIDTH, HEIGHT = 2800, 1700  # physical pixels: the other shots are 1.5x of 1360 wide, and a 200% display gives this
SETTINGS = {
    "workbench.colorTheme": "Default Light Modern",
    "workbench.startupEditor": "none",
    "workbench.statusBar.visible": False,
    "window.zoomLevel": 0,
    "chat.canvases.enabled": True,
    "telemetry.telemetryLevel": "off",
    "update.mode": "none",
    "extensions.ignoreRecommendations": True,
}
DWMWA_EXTENDED_FRAME_BOUNDS = 9


class Rect(ctypes.Structure):
    _fields_ = (("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="the repo, as `readthrough repos` names it")
    parser.add_argument("--pr", type=int, required=True, help="the PR the agent places on the map")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    if sys.platform != "win32":
        print("This script captures a window the Windows way; run it on Windows.")
        return 2

    settings = load_settings()
    cfg = settings.config_for(args.repo)
    server = running_server(settings.home)
    if server is None:
        print("The live dashboard isn't running: `readthrough serve` first.")
        return 1
    if cfg.slug.lower() not in (s.lower() for s in server["health"].get("repos", [])):
        print(f"The dashboard isn't serving {cfg.repo}.")
        return 1
    ok, detail = canvas_state()
    if not ok:
        print(f"The canvas isn't installed or is stale ({detail}): `readthrough install canvas` first.")
        return 1

    (PROFILE / "User").mkdir(parents=True, exist_ok=True)
    (PROFILE / "User" / "settings.json").write_text(json.dumps(SETTINGS, indent=2), encoding="utf-8")
    prompt = (
        f"Open the readthrough canvas on {cfg.repo} and show #{args.pr} on the map with panel none. "
        "Then ask me what I think the riskiest part of this change is, and stop."
    )
    url = "vscode://agents/new?prompt=" + urllib.parse.quote(prompt, safe="")
    launch = ["code", "--user-data-dir", str(PROFILE), "--new-window", "--disable-workspace-trust", "--open-url", url]
    print("Starting VS Code on the screenshot profile. In that window: sign in to GitHub if it asks, then press Enter")
    print("to send the prompt, and approve the canvas when the agent asks to open it.")
    subprocess.Popen(launch, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S602 - `code` is a .cmd shim; a fixed argument list, no user input in it

    print("Waiting for the canvas to connect to the dashboard...")
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if _focus(server, cfg.slug, args.pr) >= 1:
            break
        time.sleep(2)
    else:
        print("No canvas connected within 10 minutes.")
        return 1
    print("Connected. When the agent has asked its question and stopped, press Enter here.")
    input()
    _focus(server, cfg.slug, args.pr)  # the map, narrowed to the PR, whatever was clicked in between
    time.sleep(1.5)

    hwnd = _window_of_profile()
    if not hwnd:
        print("Couldn't find the screenshot profile's window.")
        return 1
    user32 = ctypes.windll.user32
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2: coordinates in physical pixels
    user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, WIDTH, HEIGHT, 0x0040)
    user32.SetForegroundWindow(wintypes.HWND(hwnd))
    time.sleep(1.5)
    rect = Rect()
    ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
    )
    image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
    width = 1800
    image = image.resize((width, round(image.height * width / image.width)))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.out, "WEBP", quality=80, method=6)
    print(f"Wrote {args.out} ({image.width}x{image.height}). The VS Code window is left for you to close.")
    return 0


def _focus(server: dict, slug: str, pr: int) -> int:
    """Point every page showing the repo at the PR on the map; returns how many pages heard."""
    request = urllib.request.Request(  # noqa: S310 - a fixed http://127.0.0.1 URL
        repo_url(server["port"], slug) + "api/focus",
        data=json.dumps({"pr": pr, "panel": "none"}).encode(),
        headers={"Content-Type": "application/json", "X-Readthrough-Token": server["token"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:  # noqa: S310 - as above
            return int(json.loads(resp.read()).get("delivered", 0))
    except OSError:
        return 0


def _window_of_profile() -> int:
    """The main window of the VS Code process started on the screenshot profile."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name = 'Code.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{PROFILE.name}*' -and $_.CommandLine -notlike '*--type=*' }} | "
        "ForEach-Object { (Get-Process -Id $_.ProcessId).MainWindowHandle }"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        encoding="utf-8",
        check=False,
    ).stdout
    handles = [int(line) for line in out.split() if line.strip().isdigit() and int(line) > 0]
    return handles[0] if handles else 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())
