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
        if path not in self.values and not any(k.startswith(path + "\\") for k in self.values):
            raise OSError(path)
        return FakeKey(self, path)

    def EnumKey(self, key: FakeKey, index: int) -> str:
        children = sorted({k[len(key.path) + 1 :].split("\\")[0] for k in self.values if k.startswith(key.path + "\\")})
        if index >= len(children):
            raise OSError("no more subkeys")
        return children[index]

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


# ---------------------------------------------------------------- the receipt, and keeping installs current


@pytest.fixture
def me(monkeypatch, tmp_path: Path) -> Path:
    """This install's interpreter: a pythonw.exe beside a python.exe, both empty (so neither is a console program)."""
    python = tmp_path / "env" / "python.exe"
    python.parent.mkdir()
    python.write_text("", encoding="utf-8")
    pythonw = python.with_name("pythonw.exe")
    pythonw.write_text("", encoding="utf-8")
    monkeypatch.setattr(system.sys, "executable", str(python))
    monkeypatch.setattr(system, "windowless_python", lambda: pythonw)
    return pythonw


def _skill(name: str = "copilot") -> tuple[str, system.PieceKind, dict]:
    return f"skill:{name}", system.PieceKind.SKILL, {"agent": name}


def test_an_install_is_recorded_with_what_removes_it_and_an_uninstall_uses_that(me):
    name, kind, args = _skill()
    where = Path(system.install(name, kind, args))
    entry = system.load_receipt()[name]
    assert (entry["kind"], entry["args"], entry["interpreter"]) == ("skill", {"agent": "copilot"}, str(me))
    assert entry["handles"] == [{"kind": "file", "target": str(where / "SKILL.md")}]

    assert system.uninstall(name, kind, args) is True
    assert not where.exists(), "the file, and the directory it was alone in"
    assert name not in system.load_receipt()
    assert system.uninstall(name, kind, args) is False, "nothing left to remove"


def test_reconcile_updates_adopts_forgets_and_retires_only_what_this_install_put_there(me, tmp_path: Path):
    copilot, claude = _skill("copilot"), _skill("claude")
    system.install(*copilot)
    skill = system.probe(copilot[1], copilot[2]).where
    Path(skill).write_text("an older version's skill", encoding="utf-8")  # an upgrade changed the bundled text
    pre_receipt = Path(system.install(*claude)) / "SKILL.md"  # as 0.5 would have left it: on disk, not recorded
    system._forget(claude[0])

    retired_file = tmp_path / "old-piece" / "SKILL.md"  # what a retired piece left, at a path only its record knows
    retired_file.parent.mkdir()
    retired_file.write_text("x", encoding="utf-8")
    foreign_file = tmp_path / "foreign" / "SKILL.md"
    foreign_file.parent.mkdir()
    foreign_file.write_text("x", encoding="utf-8")
    entries = {
        "gadget": {"kind": "gadget", "interpreter": str(me), "handles": [{"kind": "file", "target": str(retired_file)}]},
        "gizmo": {"kind": "gizmo", "interpreter": "C:/other/pythonw.exe",
                  "handles": [{"kind": "file", "target": str(foreign_file)}]},
        "canvas": {"kind": "canvas", "args": {}, "interpreter": str(me), "handles": []},  # removed by hand since
    }  # fmt: skip
    system._edit_receipt(lambda e: e.update(entries))

    changes = system.reconcile()
    assert f"updated skill:copilot to {system.__version__}" in changes
    assert Path(skill).read_text(encoding="utf-8") == system.agents.bundled_skill()
    assert "removed gadget, which this version no longer installs" in changes and not retired_file.exists()
    assert foreign_file.exists(), "another install's retired piece is that install's to remove"
    assert "canvas is no longer installed; forgot it" in changes
    receipt = system.load_receipt()
    assert claude[0] in receipt and pre_receipt.exists(), "a piece from before the receipt is adopted, not reinstalled"
    assert "gadget" not in receipt and "canvas" not in receipt and "gizmo" in receipt
    assert system.reconcile() == [], "and a second pass has nothing to do"


def test_reconcile_never_touches_another_installs_canvas(me, monkeypatch, tmp_path: Path):
    dest = system.install_canvas()
    record = dest / system.CANVAS_RECORD
    record.write_text(
        '{"argv": ["C:/elsewhere/python.exe", "-m", "readthrough"], "version": "0.1.0"}', encoding="utf-8"
    )
    assert system.probe(system.PieceKind.CANVAS, {}) == system.Found(
        True, False, False, "C:/elsewhere/python.exe -m readthrough"
    )
    assert system.reconcile() == []
    assert "0.1.0" in record.read_text(encoding="utf-8")


def test_reconcile_reregisters_this_installs_server_task_when_its_command_changed(me, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(system.sys, "platform", "win32")
    registered: list[str] = []
    task = {"execute": str(me), "arguments": "-m readthrough serve --some-old-flag"}

    def scheduler(script: str):
        if "Register-ScheduledTask" in script:
            registered.append(script)
            task["arguments"] = "-m readthrough serve"
        elif "Get-ScheduledTask " in script and "readthrough-server" in script:
            out = f"Running|||0|{task['execute']}|{task['arguments']}"
            return subprocess.CompletedProcess(["powershell"], 0, stdout=out, stderr="")
        elif "Get-ScheduledTask " in script:
            return subprocess.CompletedProcess(["powershell"], 3, stdout="", stderr="")
        return subprocess.CompletedProcess(["powershell"], 0, stdout="", stderr="")

    monkeypatch.setattr(system, "_powershell", scheduler)
    system._edit_receipt(lambda e: e.update({"server": {"kind": "server", "args": {}, "interpreter": str(me)}}))
    assert system.reconcile() == [f"updated server to {system.__version__}"] and len(registered) == 1
    assert system.reconcile() == [] and len(registered) == 1

    task["execute"] = "C:/other/pythonw.exe"  # another checkout took the task over
    task["arguments"] = "-m readthrough serve --old"
    assert system.reconcile() == [] and len(registered) == 1, "another install's task is left alone"
    piece = next(p for p in system.pieces("copilot") if p.name == "server")
    assert piece.state is False and "runs another readthrough: C:/other/pythonw.exe" in piece.detail


def test_a_tampered_receipt_removes_nothing_readthrough_did_not_write(me, tmp_path: Path):
    precious = tmp_path / "notes.txt"
    precious.write_text("keep", encoding="utf-8")
    assert system._remove(system.Handle(system.HandleKind.FILE, str(precious))) is False
    assert system._remove(system.Handle(system.HandleKind.TASK, "Microsoft Edge Update")) is False
    assert system._remove(system.Handle(system.HandleKind.REGISTRY, r"Software\Microsoft\Windows")) is False
    assert precious.exists()


def test_uninstall_all_removes_the_receipts_pieces_and_those_from_before_it(me, tmp_path: Path):
    copilot = Path(system.install(*_skill("copilot")))
    claude = Path(system.install(*_skill("claude")))
    system._forget("skill:claude")  # installed before there was a receipt
    assert sorted(system.uninstall_all()) == ["skill:claude", "skill:copilot"]
    assert not copilot.exists() and not claude.exists() and system.load_receipt() == {}
    assert system.uninstall_all() == []
