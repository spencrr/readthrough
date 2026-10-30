"""OS integration: the ``readthrough://`` URL handler and the daily refresh task, and the table of everything
``readthrough install`` can set up on this machine (``pieces``), which ``doctor`` and the setup page read too.

Windows is implemented directly (HKCU registry and Task Scheduler, no admin rights needed). On other
platforms the functions explain what to set up by hand.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from readthrough import __version__, agents
from readthrough.files import write_atomic
from readthrough.review import URL_SCHEME

if sys.platform == "win32":
    import winreg

TASK_NAME = "readthrough"
SERVER_TASK_NAME = "readthrough-server"
PROTOCOL_KEY = rf"Software\Classes\{URL_SCHEME}"

_DAYS = {"daily": None, "weekdays": "Monday,Tuesday,Wednesday,Thursday,Friday"}


def windowless_python() -> Path:
    """pythonw.exe from the same environment, so background launches don't flash a console."""
    exe = Path(sys.executable)
    if sys.platform == "win32":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return candidate
    return exe


def handler_command() -> str:
    return f'"{windowless_python()}" -m readthrough handle-url "%1"'


def _not_windows(what: str, manual: str) -> NotImplementedError:
    """The error for an installer that's automated on Windows only. Callers ``raise`` it inside a platform check."""
    return NotImplementedError(f"{what} is automated on Windows only. On this platform:\n{manual}")


# ---------------------------------------------------------------- URL protocol


def install_protocol() -> str:
    if sys.platform != "win32":
        raise _not_windows(
            "Registering the readthrough:// handler",
            "  macOS: wrap `readthrough handle-url` in an app bundle with CFBundleURLSchemes = readthrough.\n"
            "  Linux: add a .desktop file with MimeType=x-scheme-handler/readthrough and run\n"
            "         `xdg-mime default readthrough.desktop x-scheme-handler/readthrough`.",
        )
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "URL:Readthrough")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY + r"\shell\open\command") as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, handler_command())
    return handler_command()


def registered_protocol_command() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY + r"\shell\open\command") as key:
            return str(winreg.QueryValueEx(key, "")[0])  # "" is the key's default value
    except OSError:
        return None


def uninstall_protocol() -> bool:
    if sys.platform != "win32" or registered_protocol_command() is None:
        return False
    for sub in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY + sub)
    return True


# ---------------------------------------------------------------- scheduled refresh


@dataclass
class ScheduleStatus:
    installed: bool
    state: str = ""
    next_run: str = ""
    last_run: str = ""
    last_result: str = ""


def _powershell(script: str) -> subprocess.CompletedProcess[str]:
    # PowerShell writes its console code page by default; ask for UTF-8 so task names round-trip.
    script = "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n" + script
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        check=False,
    )


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def install_schedule(*, at: str, days: str, open_mode: str, home: Path | None = None) -> str:
    cron_days = "1-5" if days == "weekdays" else "*"
    hour, minute = at.split(":")
    if sys.platform != "win32":
        raise _not_windows(
            "The daily refresh",
            f"  Add this line with `crontab -e`:\n  {int(minute)} {int(hour)} * * {cron_days} readthrough build --all --open {open_mode}",
        )
    trigger = f"New-ScheduledTaskTrigger -Daily -At {_ps_quote(at)}"
    if _DAYS[days]:
        trigger = f"New-ScheduledTaskTrigger -Weekly -DaysOfWeek {_DAYS[days]} -At {_ps_quote(at)}"
    settings = "-ExecutionTimeLimit (New-TimeSpan -Minutes 30)"
    return _register_task(
        TASK_NAME,
        _module_args(home, f"build --all --open {open_mode}"),
        trigger,
        settings,
        "Refresh every readthrough repo and open the dashboard when something needs you",
    )


def install_server_task(*, home: Path | None = None) -> str:
    """Run `readthrough serve` at logon, restart it if it dies, and start it now."""
    if sys.platform != "win32":
        raise _not_windows(
            "Starting the live server at logon",
            "  Run `readthrough serve` from a launchd agent (macOS) or a systemd user service (Linux).",
        )
    trigger = "New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
    settings = "-ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew"
    command = _register_task(
        SERVER_TASK_NAME, _module_args(home, "serve"), trigger, settings, "Live Readthrough dashboard"
    )
    _powershell(f"Start-ScheduledTask -TaskName {_ps_quote(SERVER_TASK_NAME)}")
    return command


def _module_args(home: Path | None, command: str) -> str:
    args = "-m readthrough"
    if home is not None:
        args += f' --home "{home}"'
    return f"{args} {command}"


def _register_task(name: str, args: str, trigger: str, settings: str, description: str) -> str:
    script = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute {_ps_quote(str(windowless_python()))} -Argument {_ps_quote(args)}
$trigger = {trigger}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries {settings}
Register-ScheduledTask -TaskName {_ps_quote(name)} -Action $action -Trigger $trigger -Settings $settings -Description {_ps_quote(description)} -Force | Out-Null
"""
    result = _powershell(script)
    if result.returncode != 0:
        raise RuntimeError(f"Register-ScheduledTask failed: {result.stderr.strip()}")
    return f"{windowless_python()} {args}"


def schedule_status(name: str = TASK_NAME) -> ScheduleStatus:
    if sys.platform != "win32":
        return ScheduleStatus(installed=False)
    script = f"""
$t = Get-ScheduledTask -TaskName {_ps_quote(name)} -ErrorAction SilentlyContinue
if (-not $t) {{ exit 3 }}
$i = $t | Get-ScheduledTaskInfo
"$($t.State)|$($i.NextRunTime)|$($i.LastRunTime)|$($i.LastTaskResult)"
"""
    result = _powershell(script)
    if result.returncode != 0:
        return ScheduleStatus(installed=False)
    state, next_run, last_run, last_result = [*result.stdout.strip().split("|"), "", "", "", ""][:4]
    return ScheduleStatus(True, state, next_run, last_run, last_result)


def run_schedule_now() -> None:
    result = _powershell(f"Start-ScheduledTask -TaskName {_ps_quote(TASK_NAME)}")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Start-ScheduledTask failed")


def uninstall_schedule(name: str = TASK_NAME) -> bool:
    if not schedule_status(name).installed:
        return False
    _powershell(f"Stop-ScheduledTask -TaskName {_ps_quote(name)}")
    result = _powershell(f"Unregister-ScheduledTask -TaskName {_ps_quote(name)} -Confirm:$false")
    return result.returncode == 0


def message_box(title: str, text: str) -> None:
    """Show errors from windowless launches, which otherwise fail silently."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)


# ---------------------------------------------------------------- what install can set up


@dataclass(frozen=True)
class Piece:
    """One thing ``readthrough install`` can set up on this machine, as ``doctor`` and ``install`` both see it.

    ``state`` is ``True`` (installed and current), ``False`` (missing or stale), or ``None`` (not applicable
    here: the agent isn't installed, the platform has no automation for it). ``recommended`` says whether the
    interactive install proposes it by default; the daily nudge isn't, since it opens a browser on a timer.
    """

    name: str  # ``skill:<agent>``, or the single-piece command's name
    label: str
    what: str  # one line a person reads before saying yes
    state: bool | None
    detail: str
    fix: str  # the single-piece command
    recommended: bool = True


def automated_here() -> bool:
    """Whether this platform has the automation for the OS pieces (the link handler and the two scheduled tasks)."""
    return sys.platform == "win32"


def pieces(agent: str, agent_options: dict[str, Any] | None = None) -> list[Piece]:
    """Every piece, in the order ``doctor`` lists them: the skill for each installed agent, then the OS pieces."""
    out: list[Piece] = []
    chosen = agents.resolve(agent, agent_options)
    skill_text = agents.bundled_skill()
    for candidate in agents.all_agents(agent_options):
        if not (candidate.available() or candidate.name == chosen.name):
            continue
        skill = candidate.skill_dir / "SKILL.md"
        current = skill.exists() and skill.read_text(encoding="utf-8") == skill_text
        out.append(Piece(
            f"skill:{candidate.name}", f"skill ({candidate.name})",
            f"teach {candidate.label} the deep-review routine ({skill})",
            current if candidate.available() else None,
            str(skill) if skill.exists() else "not installed", f"readthrough install skill --agent {candidate.name}",
        ))  # fmt: skip
    # The canvas is for Copilot hosts; it only makes sense where the Copilot CLI or its home is.
    copilot_here = agents.make("copilot", agent_options).available() or agents.copilot_home().exists()
    canvas_ok, canvas_detail = canvas_state()
    out.append(Piece(
        "canvas", "canvas",
        "show the dashboard beside agent sessions in VS Code's Agents window, steered by the agent (experimental)",
        (canvas_ok is True) if copilot_here else None, canvas_detail, "readthrough install canvas",
    ))  # fmt: skip
    automated = automated_here()
    registered = registered_protocol_command()
    out.append(Piece(
        "protocol", "protocol", f"register {URL_SCHEME}:// links, so the dashboard's buttons work in one click",
        (registered == handler_command()) if automated else None,
        registered or "not registered", "readthrough install protocol",
    ))  # fmt: skip
    task = schedule_status(SERVER_TASK_NAME)
    out.append(Piece(
        "server", "server", "start the live dashboard at logon, so it is always fresh and always up",
        task.installed if automated else None,
        "starts at logon" if task.installed else "not installed", "readthrough install server",
    ))  # fmt: skip
    sched = schedule_status()
    out.append(Piece(
        "schedule", "schedule", "open the dashboard each weekday morning when something needs you (the nudge)",
        sched.installed if automated else None,
        f"{sched.state}, next {sched.next_run}, last result {sched.last_result}" if sched.installed else "not installed",
        "readthrough install schedule", recommended=False,
    ))  # fmt: skip
    return out


# ---------------------------------------------------------------- the Copilot canvas


CANVAS_FILES = ("extension.mjs", "readthrough.mjs")
CANVAS_RECORD = "readthrough.json"  # how the extension runs readthrough: the installing interpreter, and the version


def canvas_dir() -> Path:
    """Where the Copilot runtime loads a user's extensions from."""
    return agents.copilot_home() / "extensions" / "readthrough"


def canvas_record() -> dict[str, Any]:
    return {"argv": [sys.executable, "-m", "readthrough"], "version": __version__}


def canvas_state(dest: Path | None = None) -> tuple[bool | None, str]:
    """Whether the installed canvas is this readthrough's: ``None`` when absent, ``False`` when another version
    or interpreter installed it (the recorded argv is what it will run)."""
    dest = dest or canvas_dir()
    record_path = dest / CANVAS_RECORD
    if not record_path.exists() or not all((dest / name).exists() for name in CANVAS_FILES):
        return None, "not installed"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        current = record.get("argv") == canvas_record()["argv"] and record.get("version") == __version__
        return bool(
            current
        ), f"{dest} (version {record.get('version', '?')}, runs {' '.join(record.get('argv', ['?']))})"
    except (OSError, ValueError, AttributeError):
        return False, f"{dest} (its record is unreadable)"


def install_canvas(dest: Path | None = None) -> Path:
    """Copy the extension beside a record of how to run this readthrough; atomic per file, so a host reloading
    its extensions mid-install never reads half a module."""
    dest = dest or canvas_dir()
    dest.mkdir(parents=True, exist_ok=True)
    for name in CANVAS_FILES:
        write_atomic(dest / name, files("readthrough").joinpath("assets/extension", name).read_text(encoding="utf-8"))
    write_atomic(dest / CANVAS_RECORD, json.dumps(canvas_record(), indent=2) + "\n")
    return dest


def uninstall_canvas(dest: Path | None = None) -> bool:
    """Remove only the files the install wrote, and the directory if nothing else is in it. Says whether there was
    anything to remove; raises ``OSError`` naming what couldn't be."""
    dest = dest or canvas_dir()
    ours = [dest / name for name in (*CANVAS_FILES, CANVAS_RECORD)]
    removed = any(p.exists() for p in ours)
    for p in ours:
        p.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        dest.rmdir()
    if left := [p for p in ours if p.exists()]:
        raise OSError(", ".join(str(p) for p in left))
    return removed
