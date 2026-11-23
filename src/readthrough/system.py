"""OS integration: the ``readthrough://`` URL handler and the daily refresh task, and the table of everything
``readthrough install`` can set up on this machine (``pieces``), which ``doctor`` and the setup page read too.

Windows is implemented directly (HKCU registry and Task Scheduler, no admin rights needed). On other
platforms the functions explain what to set up by hand.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import itertools
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any, assert_never

from readthrough import __version__, agents
from readthrough.files import locked, write_atomic
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


def opens_a_console(exe: Path) -> bool:
    """Whether a Windows program is built for the console subsystem, so launching it opens a window.

    A venv's pythonw.exe should be a GUI-subsystem launcher; uv before 0.13 wrote a console one, and a
    server or link handler started through it shows a console window at every logon and click.
    """
    try:
        with exe.open("rb") as f:
            header_offset = int.from_bytes(f.read(0x40)[0x3C:0x40], "little")
            f.seek(header_offset)
            pe = f.read(24 + 70)  # signature, COFF header, then the optional header up to Subsystem
    except (OSError, ValueError):
        return False
    return pe[:4] == b"PE\0\0" and int.from_bytes(pe[24 + 68 : 24 + 70], "little") == 3  # IMAGE_SUBSYSTEM_WINDOWS_CUI


def console_note() -> str:
    """Why a windowless launch would still show a window, for doctor; ``""`` when it wouldn't."""
    exe = windowless_python()
    if sys.platform != "win32" or exe.name != "pythonw.exe" or not opens_a_console(exe):
        return ""
    return f"{exe} is a console program (an older uv made this environment): the server and link handler open a window"


# A windowless launcher that is really a console program can't be worked around from here (a GUI entry point still
# runs it), so the environment has to be made again by a uv that writes a GUI one (0.13 does).
CONSOLE_FIX = (
    "recreate the environment: in a checkout, `uv venv --clear` then `uv sync`; for a tool install, "
    "`uv tool uninstall readthrough` then `uv tool install readthrough`"
)


def _windowless_or_refuse(what: str) -> None:
    """Refuse to register something that would open a console window every time Windows starts it."""
    if note := console_note():
        raise RuntimeError(f"Not {what}: {note}. First {CONSOLE_FIX}.")


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
    _windowless_or_refuse("registering the readthrough:// handler")
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
    return _delete_key(PROTOCOL_KEY)


def _delete_key(path: str) -> bool:
    """Delete an HKCU key and everything under it; False when there was none."""
    if sys.platform != "win32":
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            children: list[str] = []
            with contextlib.suppress(OSError):  # EnumKey raises once there are no more
                children.extend(winreg.EnumKey(key, i) for i in itertools.count())
    except OSError:
        return False
    for child in children:
        _delete_key(f"{path}\\{child}")
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    return True


# ---------------------------------------------------------------- scheduled refresh


@dataclass
class ScheduleStatus:
    installed: bool
    state: str = ""
    next_run: str = ""
    last_run: str = ""
    last_result: str = ""
    execute: str = ""  # the program the task runs, which says whose readthrough it is
    arguments: str = ""


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
    _windowless_or_refuse(f"registering the {name} task")
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
"$($t.State)|$($i.NextRunTime)|$($i.LastRunTime)|$($i.LastTaskResult)|$($t.Actions[0].Execute)|$($t.Actions[0].Arguments)"
"""
    result = _powershell(script)
    if result.returncode != 0:
        return ScheduleStatus(installed=False)
    fields = [*result.stdout.strip().split("|", 5), *[""] * 6][:6]  # the arguments come last: they may hold a "|"
    return ScheduleStatus(True, *fields)


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
    server = probe(PieceKind.SERVER, dict(load_receipt().get("server", {}).get("args") or {}))
    if not server.present:
        server_detail = "not installed"
    elif not server.ours:
        server_detail = f"starts at logon, but runs another readthrough: {server.where}"
    else:
        server_detail = "starts at logon" + ("" if server.fresh else f"; out of date ({server.where})")
    out.append(Piece(
        "server", "server", "start the live dashboard at logon, so it is always fresh and always up",
        server.fresh if automated else None, server_detail, "readthrough install server",
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


# ---------------------------------------------------------------- what was installed, and keeping it current
#
# What `install` puts on the machine is derived state: this version, this interpreter, and the options it was
# given say what each piece should look like. So there are no migrations to run in order. Each install is
# recorded in a receipt with how to remove it, and `reconcile` brings the machine in line with the running
# version: it updates what's out of date, removes what an earlier version installed and this one no longer
# does (from the receipt alone, so no code for a retired piece has to survive it), and forgets what someone
# removed by hand. The machine is the truth; the receipt only says where to look and how a piece was asked for.


class PieceKind(StrEnum):
    SKILL = "skill"
    CANVAS = "canvas"
    PROTOCOL = "protocol"
    SERVER = "server"
    SCHEDULE = "schedule"


class HandleKind(StrEnum):
    """What an installed piece left on the machine: what any later version needs in order to remove it."""

    TASK = "task"  # a scheduled task, by name
    REGISTRY = "registry"  # an HKCU key, with everything under it
    FILE = "file"  # a file it wrote; its directory goes too once nothing else is in it


@dataclass(frozen=True)
class Handle:
    kind: HandleKind
    target: str


# What a handle from the receipt may remove. The receipt is a file in the user's home, so it is read as input:
# only files readthrough writes, keys under Software\Classes, and tasks named for readthrough.
OUR_FILES = frozenset({"SKILL.md", *CANVAS_FILES, CANVAS_RECORD})


@dataclass(frozen=True)
class Found:
    """A piece as it is on the machine."""

    present: bool
    ours: bool = False  # put there by this interpreter, so this install may change or remove it
    fresh: bool = False  # and as this version, with the options recorded, would put it there
    where: str = ""  # what it runs or where it is, for doctor


def receipt_path() -> Path:
    """The install receipt. The pieces belong to the OS user, not to whichever home a command names, so it lives in
    the default home whatever ``--home`` says."""
    return Path(os.environ.get("READTHROUGH_HOME") or Path.home() / ".readthrough") / "installed.json"


def load_receipt() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(receipt_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = data.get("pieces") if isinstance(data, dict) else None
    return {k: v for k, v in entries.items() if isinstance(v, dict)} if isinstance(entries, dict) else {}


def _edit_receipt(change: Callable[[dict[str, dict[str, Any]]], object]) -> None:
    path = receipt_path()
    with locked(path):
        entries = load_receipt()
        change(entries)
        write_atomic(path, json.dumps({"schema": 1, "pieces": entries}, indent=1, sort_keys=True) + "\n")


def _record(name: str, kind: PieceKind, args: dict[str, Any], agent_options: dict[str, Any] | None) -> None:
    entry = {
        "kind": kind.value,
        "args": args,
        "version": __version__,
        "interpreter": str(windowless_python()),
        "recorded": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "handles": [{"kind": h.kind.value, "target": h.target} for h in handles(kind, args, agent_options)],
    }
    _edit_receipt(lambda entries: entries.__setitem__(name, entry))


def _forget(name: str) -> None:
    _edit_receipt(lambda entries: entries.pop(name, None))


def _recorded_handles(entry: dict[str, Any]) -> list[Handle]:
    out = []
    for h in entry.get("handles") or []:
        try:
            out.append(Handle(HandleKind(h["kind"]), str(h["target"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _same_program(a: str | Path, b: str | Path) -> bool:
    # Not resolved: a POSIX venv's python is a symlink to the base interpreter, which every venv shares.
    return os.path.normcase(Path(a).absolute()) == os.path.normcase(Path(b).absolute())


def _skill_file(args: dict[str, Any], agent_options: dict[str, Any] | None) -> Path:
    dest = Path(args["dest"]) if args.get("dest") else agents.make(args["agent"], agent_options).skill_dir
    return dest / "SKILL.md"


def _canvas_dest(args: dict[str, Any]) -> Path:
    return Path(args["dest"]) if args.get("dest") else canvas_dir()


def _home(args: dict[str, Any]) -> Path | None:
    return Path(args["home"]) if args.get("home") else None


def _task_arguments(kind: PieceKind, args: dict[str, Any]) -> str:
    command = "serve" if kind == PieceKind.SERVER else f"build --all --open {args.get('open_mode', 'auto')}"
    return _module_args(_home(args), command)


def handles(kind: PieceKind, args: dict[str, Any], agent_options: dict[str, Any] | None = None) -> list[Handle]:
    """Where a piece lives, as this version installs it."""
    match kind:
        case PieceKind.SKILL:
            return [Handle(HandleKind.FILE, str(_skill_file(args, agent_options)))]
        case PieceKind.CANVAS:
            dest = _canvas_dest(args)
            return [Handle(HandleKind.FILE, str(dest / name)) for name in (*CANVAS_FILES, CANVAS_RECORD)]
        case PieceKind.PROTOCOL:
            return [Handle(HandleKind.REGISTRY, PROTOCOL_KEY)]
        case PieceKind.SERVER:
            return [Handle(HandleKind.TASK, SERVER_TASK_NAME)]
        case PieceKind.SCHEDULE:
            return [Handle(HandleKind.TASK, TASK_NAME)]
        case _:
            assert_never(kind)


def probe(kind: PieceKind, args: dict[str, Any], agent_options: dict[str, Any] | None = None) -> Found:
    """Whether a piece is on the machine, whether this interpreter put it there, and whether it is current."""
    me = windowless_python()
    match kind:
        case PieceKind.SKILL:
            path = _skill_file(args, agent_options)
            if not path.exists():
                return Found(present=False)
            return Found(True, True, path.read_text(encoding="utf-8") == agents.bundled_skill(), str(path))
        case PieceKind.CANVAS:
            record_path = _canvas_dest(args) / CANVAS_RECORD
            if not record_path.exists():
                return Found(present=False)
            try:
                argv = json.loads(record_path.read_text(encoding="utf-8")).get("argv") or []
            except (OSError, ValueError, AttributeError):
                return Found(True, True, False, str(record_path.parent))  # ours by its place; reinstalling fixes it
            ours = bool(argv) and _same_program(str(argv[0]), sys.executable)
            return Found(True, ours, ours and canvas_state(record_path.parent)[0] is True, " ".join(map(str, argv)))
        case PieceKind.PROTOCOL:
            command = registered_protocol_command()
            if command is None:
                return Found(present=False)
            ours = os.path.normcase(str(me)) in os.path.normcase(command)
            return Found(True, ours, command == handler_command(), command)
        case PieceKind.SERVER | PieceKind.SCHEDULE:
            status = schedule_status(SERVER_TASK_NAME if kind == PieceKind.SERVER else TASK_NAME)
            if not status.installed:
                return Found(present=False)
            ours = bool(status.execute) and _same_program(status.execute, me)
            fresh = ours and status.arguments == _task_arguments(kind, args)
            return Found(True, ours, fresh, f"{status.execute} {status.arguments}".strip())
        case _:
            assert_never(kind)


def _put(kind: PieceKind, args: dict[str, Any], agent_options: dict[str, Any] | None) -> str:
    match kind:
        case PieceKind.SKILL:
            path = _skill_file(args, agent_options)
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, agents.bundled_skill())
            return str(path.parent)
        case PieceKind.CANVAS:
            return str(install_canvas(_canvas_dest(args)))
        case PieceKind.PROTOCOL:
            return install_protocol()
        case PieceKind.SERVER:
            return install_server_task(home=_home(args))
        case PieceKind.SCHEDULE:
            return install_schedule(at=args["at"], days=args["days"], open_mode=args["open_mode"], home=_home(args))
        case _:
            assert_never(kind)


def _remove(handle: Handle) -> bool:
    match handle.kind:
        case HandleKind.TASK:
            return handle.target.startswith("readthrough") and uninstall_schedule(handle.target)
        case HandleKind.REGISTRY:
            return handle.target.startswith("Software\\Classes\\") and _delete_key(handle.target)
        case HandleKind.FILE:
            path = Path(handle.target)
            if path.name not in OUR_FILES or not path.exists():
                return False
            path.unlink()
            with contextlib.suppress(OSError):
                path.parent.rmdir()  # only if nothing else is in it
            return True
        case _:
            assert_never(handle.kind)


def _remove_all(targets: list[Handle]) -> bool:
    """Remove every one, not up to the first that was there; says whether any was."""
    results = [_remove(h) for h in dict.fromkeys(targets)]
    return True in results


def install(name: str, kind: PieceKind, args: dict[str, Any], agent_options: dict[str, Any] | None = None) -> str:
    """Put one piece on the machine and record it; returns what it runs, or where it went."""
    done = _put(kind, args, agent_options)
    _record(name, kind, args, agent_options)
    return done


def uninstall(name: str, kind: PieceKind, args: dict[str, Any], agent_options: dict[str, Any] | None = None) -> bool:
    """Remove one piece: what its receipt entry says it left, else where this version would have put it."""
    entry = load_receipt().get(name)
    removed = _remove_all((_recorded_handles(entry) if entry else []) or handles(kind, args, agent_options))
    if entry:
        _forget(name)
    return removed


def _defaults(agent_options: dict[str, Any] | None) -> dict[str, tuple[PieceKind, dict[str, Any]]]:
    """Each piece as an install with no options would ask for it: how a piece from before the receipt is found.
    Not the schedule: when it runs was the user's choice, and only the receipt remembers it."""
    out = {f"skill:{a.name}": (PieceKind.SKILL, {"agent": a.name}) for a in agents.all_agents(agent_options)}
    out |= {k.value: (k, {}) for k in (PieceKind.CANVAS, PieceKind.PROTOCOL, PieceKind.SERVER)}
    return out


def reconcile(agent_options: dict[str, Any] | None = None) -> list[str]:
    """Bring what this install put on the machine in line with this version, and say what changed, a line each.

    Updates what is out of date, removes what an earlier version installed that this one doesn't, forgets what
    someone removed by hand, and records what an earlier version installed before there was a receipt. It never
    adds a piece, and never touches one another readthrough installed (another checkout, a tool install): that is
    for an explicit ``install`` there or here. The skill has no interpreter; whichever readthrough runs last writes it.
    """
    me = windowless_python()
    receipt = load_receipt()
    changes: list[str] = []
    wanted: dict[str, tuple[PieceKind, dict[str, Any]]] = {}
    for name, entry in receipt.items():
        if not _same_program(str(entry.get("interpreter") or ""), me):
            continue
        try:
            wanted[name] = (PieceKind(entry.get("kind")), dict(entry.get("args") or {}))
        except ValueError:  # a piece this version doesn't know: retired, and removed by what the receipt says
            _remove_all(_recorded_handles(entry))
            _forget(name)
            changes.append(f"removed {name}, which this version no longer installs")
    for name, piece in _defaults(agent_options).items():
        wanted.setdefault(name, piece)
    for name, (kind, args) in wanted.items():
        try:
            found = probe(kind, args, agent_options)
            if not found.present:
                if name in receipt:
                    _forget(name)
                    changes.append(f"{name} is no longer installed; forgot it")
            elif not found.ours:
                continue
            elif not found.fresh:
                install(name, kind, args, agent_options)
                changes.append(f"updated {name} to {__version__}")
            elif receipt.get(name, {}).get("version") != __version__:
                _record(name, kind, args, agent_options)
        except (OSError, RuntimeError, NotImplementedError) as exc:
            changes.append(f"couldn't update {name}: {exc}")
    return changes


def uninstall_all(agent_options: dict[str, Any] | None = None) -> list[str]:
    """Remove everything readthrough put on this machine, whichever install put it there: what the receipt lists,
    and each piece where this version would have put it. Says what was removed."""
    every = {name: _recorded_handles(entry) for name, entry in load_receipt().items()}
    for name, (kind, args) in {**_defaults(agent_options), PieceKind.SCHEDULE.value: (PieceKind.SCHEDULE, {})}.items():
        every.setdefault(name, []).extend(handles(kind, args, agent_options))
    removed = [name for name, targets in every.items() if _remove_all(targets)]
    _edit_receipt(lambda entries: entries.clear())
    return removed
