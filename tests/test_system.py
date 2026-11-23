from __future__ import annotations

import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from readthrough import system


def test_ps_quote_and_handler_command(monkeypatch, tmp_path: Path):
    assert system._ps_quote("Bob's laptop") == "'Bob''s laptop'"

    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_text("", encoding="utf-8")
    monkeypatch.setattr(system.sys, "executable", str(python))
    monkeypatch.setattr(system.sys, "platform", "win32")
    assert system.windowless_python() == pythonw
    assert system.handler_command() == f'"{pythonw}" -m readthrough handle-url "%1"'

    pythonw.unlink()
    assert system.windowless_python() == python


def _pe(subsystem: int) -> bytes:
    """The least of a Windows executable: a DOS header pointing at a PE header whose Subsystem is ``subsystem``."""
    dos = bytearray(64)
    dos[0x3C:0x40] = (64).to_bytes(4, "little")
    optional = bytearray(70)
    optional[68:70] = subsystem.to_bytes(2, "little")
    return bytes(dos) + b"PE\0\0" + bytes(20) + bytes(optional)


def test_a_console_pythonw_is_reported(monkeypatch, tmp_path: Path):
    pythonw = tmp_path / "pythonw.exe"
    monkeypatch.setattr(system.sys, "platform", "win32")
    monkeypatch.setattr(system, "windowless_python", lambda: pythonw)

    pythonw.write_bytes(_pe(2))  # GUI: what pythonw.exe should be
    assert not system.opens_a_console(pythonw)
    assert system.console_note() == ""

    pythonw.write_bytes(_pe(3))  # console: what uv before 0.13 wrote
    assert system.opens_a_console(pythonw)
    assert "console program" in system.console_note()
    # ...and nothing that Windows would start through it gets registered: each launch would open a window.
    for install in (
        system.install_protocol,
        system.install_server_task,
        lambda: system.install_schedule(at="08:30", days="daily", open_mode="auto"),
    ):
        with pytest.raises(RuntimeError, match="uv venv --clear"):
            install()

    pythonw.write_bytes(b"MZ")  # not a PE file at all: no claim
    assert not system.opens_a_console(pythonw)
    assert not system.opens_a_console(tmp_path / "missing.exe")
    monkeypatch.setattr(system.sys, "platform", "linux")
    assert system.console_note() == ""


def test_non_windows_branches(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "linux")
    assert system.registered_protocol_command() is None
    assert system.schedule_status().installed is False
    system.message_box("title", "text")

    with pytest.raises(NotImplementedError, match="Registering the readthrough:// handler"):
        system.install_protocol()
    with pytest.raises(NotImplementedError, match="crontab -e"):
        system.install_schedule(at="08:30", days="weekdays", open_mode="needed")
    with pytest.raises(NotImplementedError, match="launchd agent"):
        system.install_server_task()


class FakeKey:
    def __init__(self, module: FakeWinreg, path: str):
        self.module = module
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class FakeWinreg(ModuleType):
    HKEY_CURRENT_USER = object()
    REG_SZ = 1

    def __init__(self):
        super().__init__("winreg")
        self.values: dict[str, dict[str, str]] = {}
        self.deleted: list[str] = []

    def CreateKey(self, root, path: str):
        self.values.setdefault(path, {})
        return FakeKey(self, path)

    def OpenKey(self, root, path: str):
        if path not in self.values:
            raise OSError(path)
        return FakeKey(self, path)

    def SetValueEx(self, key: FakeKey, name, reserved, kind, value: str):
        self.values.setdefault(key.path, {})["" if name is None else name] = value

    def QueryValueEx(self, key: FakeKey, name: str):
        return self.values[key.path][name], self.REG_SZ

    def DeleteKey(self, root, path: str):
        self.deleted.append(path)
        self.values.pop(path, None)


def test_protocol_registry_helpers_use_hkcu_only(monkeypatch, tmp_path: Path):
    fake = FakeWinreg()
    monkeypatch.setattr(system, "winreg", fake, raising=False)  # absent on other platforms, so don't require it
    monkeypatch.setattr(system.sys, "platform", "win32")
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(system.sys, "executable", str(python))

    command = system.install_protocol()
    assert command == f'"{python}" -m readthrough handle-url "%1"'
    assert fake.values[system.PROTOCOL_KEY][""] == "URL:Readthrough"
    assert fake.values[system.PROTOCOL_KEY]["URL Protocol"] == ""
    assert system.registered_protocol_command() == command
    assert system.uninstall_protocol() is True
    assert fake.deleted == [
        system.PROTOCOL_KEY + r"\shell\open\command",
        system.PROTOCOL_KEY + r"\shell\open",
        system.PROTOCOL_KEY + r"\shell",
        system.PROTOCOL_KEY,
    ]
    assert system.uninstall_protocol() is False


def test_schedule_registration_status_and_actions(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(system.sys, "platform", "win32")
    python = tmp_path / "pythonw.exe"
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(system, "windowless_python", lambda: python)
    calls = []

    def ok(script: str):
        calls.append(script)
        stdout = "Ready|2026-10-06 08:00|2026-10-05 08:00|0" if "Get-ScheduledTaskInfo" in script else ""
        return subprocess.CompletedProcess(["powershell"], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(system, "_powershell", ok)
    command = system.install_schedule(at="08:30", days="weekdays", open_mode="needed", home=tmp_path)
    assert command == f'{python} -m readthrough --home "{tmp_path}" build --all --open needed'
    assert "New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday" in calls[-1]
    assert "Register-ScheduledTask -TaskName 'readthrough'" in calls[-1]

    server_command = system.install_server_task(home=tmp_path)
    assert server_command == f'{python} -m readthrough --home "{tmp_path}" serve'
    assert calls[-1] == "Start-ScheduledTask -TaskName 'readthrough-server'"

    status = system.schedule_status()
    assert (status.installed, status.state, status.next_run, status.last_run, status.last_result) == (
        True,
        "Ready",
        "2026-10-06 08:00",
        "2026-10-05 08:00",
        "0",
    )
    system.run_schedule_now()
    assert calls[-1] == "Start-ScheduledTask -TaskName 'readthrough'"
    assert system.uninstall_schedule() is True
    assert calls[-2] == "Stop-ScheduledTask -TaskName 'readthrough'"
    assert calls[-1] == "Unregister-ScheduledTask -TaskName 'readthrough' -Confirm:$false"


def test_schedule_failures(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(system.sys, "platform", "win32")
    monkeypatch.setattr(system, "windowless_python", lambda: tmp_path / "pythonw.exe")

    monkeypatch.setattr(
        system,
        "_powershell",
        lambda script: subprocess.CompletedProcess(["powershell"], 9, stdout="", stderr="denied"),
    )
    with pytest.raises(RuntimeError, match="Register-ScheduledTask failed: denied"):
        system.install_schedule(at="09:00", days="daily", open_mode="always")
    assert system.schedule_status().installed is False
    assert system.uninstall_schedule() is False
    with pytest.raises(RuntimeError, match="denied"):
        system.run_schedule_now()
