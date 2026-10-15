"""Terminal launchers: how a deep-review session gets its own window.

Each launcher turns a command (argv, working directory, title) into a ``Spawn``: the process to start and
how. ``terminal = "auto"`` in ``config.toml`` tries the platform's launchers in order and uses the first
one that is installed; naming one (``terminal = "wt"``) uses it even when something else is available.
Options go in ``[terminals.<name>]`` and are validated against the launcher's options dataclass.

Only the Windows launchers are exercised on the author's machine. The macOS and Linux ones are the
invocations those terminals document; if one misbehaves, ``readthrough review N --here`` runs the
session in the current terminal instead.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from readthrough.registry import NoOptions, Registry, RegistryError

AUTO = "auto"


@dataclass(frozen=True)
class Command:
    """What to run: the program and its arguments, where, and a window title."""

    argv: list[str]
    cwd: Path
    title: str


@dataclass(frozen=True)
class Spawn:
    """What to start to get that command into a new window."""

    argv: list[str]
    cwd: Path | None = None
    creationflags: int = 0


@dataclass(frozen=True)
class Launcher:
    """``program`` must be on PATH for the launcher to count as available; None means nothing extra is needed.

    ``spawn`` gets the program's path, the command, and the launcher's validated options object.
    """

    program: str | None
    spawn: Callable[[str, Command, Any], Spawn]
    platforms: tuple[str, ...]

    def path(self) -> str | None:
        return "" if self.program is None else shutil.which(self.program)

    def available(self) -> bool:
        return sys.platform in self.platforms and self.path() is not None


TERMINALS: Registry[Launcher] = Registry("terminal")

# Tried in this order by "auto", filtered to the current platform.
AUTO_ORDER = ("wt", "console", "terminal-app", "x-terminal-emulator", "gnome-terminal")


@dataclass(frozen=True)
class WindowsTerminalOptions:
    profile: str = ""  # a Windows Terminal profile name; empty means the default profile
    window: str = "new"  # "new" opens a window; "0" (or another id) adds a tab to that window
    extra_args: list[str] = field(default_factory=list)  # passed to `wt new-tab` as they are


def _wt(path: str, command: Command, options: WindowsTerminalOptions) -> Spawn:
    tab = ["new-tab", "--title", command.title, "-d", str(command.cwd)]
    if options.profile:
        tab += ["-p", options.profile]
    tab += options.extra_args
    # Windows Terminal treats a bare `;` as a command separator.
    return Spawn([path, "-w", options.window, *tab, *(a.replace(";", r"\;") for a in command.argv)])


TERMINALS.add("wt", "Windows Terminal, a new window or tab", options=WindowsTerminalOptions)(
    Launcher("wt", _wt, ("win32",))
)


def _console(path: str, command: Command, options: NoOptions) -> Spawn:
    return Spawn(command.argv, cwd=command.cwd, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


TERMINALS.add("console", "a plain Windows console window")(Launcher(None, _console, ("win32",)))


def _applescript_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _terminal_app(path: str, command: Command, options: NoOptions) -> Spawn:
    script = f"cd {shlex.quote(str(command.cwd))} && {shlex.join(command.argv)}"
    applescript = f'tell application "Terminal" to do script {_applescript_str(script)}'
    return Spawn([path, "-e", applescript, "-e", 'tell application "Terminal" to activate'])


TERMINALS.add("terminal-app", "macOS Terminal.app, through osascript")(
    Launcher("osascript", _terminal_app, ("darwin",))
)


def _dash_dash(path: str, command: Command, options: NoOptions) -> Spawn:
    return Spawn([path, "--", *command.argv], cwd=command.cwd)


def _dash_e(path: str, command: Command, options: NoOptions) -> Spawn:
    return Spawn([path, "-e", shlex.join(command.argv)], cwd=command.cwd)


TERMINALS.add("x-terminal-emulator", "the Debian/Ubuntu default terminal (update-alternatives)")(
    Launcher("x-terminal-emulator", _dash_e, ("linux",))
)
TERMINALS.add("gnome-terminal", "GNOME Terminal")(Launcher("gnome-terminal", _dash_dash, ("linux",)))


def choose(name: str = AUTO) -> tuple[str, Launcher]:
    """The launcher to use: the named one, or the first available one for this platform."""
    if name != AUTO:
        launcher = TERMINALS.get(name).impl
        if sys.platform not in launcher.platforms:
            raise RegistryError(f"terminal {name!r} is for {', '.join(launcher.platforms)}, not {sys.platform}")
        if launcher.path() is None:
            raise RegistryError(f"terminal {name!r} needs `{launcher.program}` on PATH")
        return name, launcher
    for candidate in AUTO_ORDER:
        launcher = TERMINALS.get(candidate).impl
        if launcher.available():
            return candidate, launcher
    raise RegistryError("No terminal emulator found. Run `readthrough review N --here` instead.")


def launch(command: Command, *, terminal: str = AUTO, options: Mapping[str, object] | None = None) -> str:
    """Open ``command`` in a new terminal window. Returns the launcher's name.

    ``options`` maps launcher names to their validated options objects (``Settings.terminal_options``).
    """
    name, launcher = choose(terminal)
    spawn = plan(name, launcher, command, options)
    subprocess.Popen(spawn.argv, cwd=spawn.cwd, creationflags=spawn.creationflags)
    return name


def plan(name: str, launcher: Launcher, command: Command, options: Mapping[str, object] | None = None) -> Spawn:
    """The process a launcher would start. Separate from ``launch`` so tests can check it without spawning."""
    opts = (options or {}).get(name) or TERMINALS.defaults_for(name)
    return launcher.spawn(launcher.path() or "", command, opts)


def describe(terminal: str = AUTO) -> str:
    """For `doctor`: which launcher would be used, or why none can be."""
    try:
        name, launcher = choose(terminal)
    except RegistryError as exc:
        return str(exc)
    where = launcher.path() or "built in"
    return f"{name} ({where})" + (", chosen automatically" if terminal == AUTO else "")
