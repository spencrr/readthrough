"""OS integration: the ``readthrough://`` URL handler and the daily refresh task.

Windows is implemented directly (HKCU registry and Task Scheduler, no admin rights needed). On other
platforms the functions explain what to set up by hand.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

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
