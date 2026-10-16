"""The review-agent registry: choosing an agent, launching sessions, and reading each agent's session store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from readthrough import agents
from readthrough.agents import AgentOptions, Claude, Copilot, SessionRecord
from readthrough.config import ConfigError, load_settings


def test_auto_takes_the_first_installed_agent(monkeypatch):
    assert agents.resolve("auto").name == "copilot"
    monkeypatch.delenv("READTHROUGH_COPILOT")
    monkeypatch.setattr("readthrough.process.shutil.which", lambda name: None)
    assert agents.resolve("auto").name == "claude", "Copilot isn't installed, Claude Code is"
    monkeypatch.delenv("READTHROUGH_CLAUDE")
    chosen = agents.resolve("auto")
    assert chosen.name == "copilot" and not chosen.available(), (
        "nothing installed: the first choice, so messages can say so"
    )
    assert "isn't installed" in agents.describe("auto") and "winget" in agents.describe("auto")
    assert agents.describe("claude").startswith("Claude Code: claude isn't installed")


def test_agent_names_and_options_are_validated_in_config(tmp_path: Path):
    (tmp_path / "config.toml").write_text('agent = "codex"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown agent 'codex'"):
        load_settings(tmp_path)
    (tmp_path / "config.toml").write_text('agent = "claude"\n[agents.claude]\nextra_arg = ["--x"]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="did you mean 'extra_args'"):
        load_settings(tmp_path)
    (tmp_path / "config.toml").write_text(
        'agent = "claude"\n[agents.claude]\nextra_args = ["--model", "opus"]\n[[repos]]\nname = "o/r"\n',
        encoding="utf-8",
    )
    settings = load_settings(tmp_path)
    cfg = settings.config_for("o/r")
    assert cfg.agent == "claude" and cfg.agent_options["claude"].extra_args == ["--model", "opus"]
    argv = agents.resolve(cfg.agent, cfg.agent_options).new_session_argv(
        session_id="s", name="n", prompt="p", add_dirs=[Path("d")], extra=["--verbose"]
    )
    assert argv[1:] == ["--session-id", "s", "--name", "n", "--add-dir", "d", "--model", "opus", "--verbose", "p"]
    settings.save()
    assert 'agent = "claude"' in (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "[agents.claude]" in (tmp_path / "config.toml").read_text(encoding="utf-8")


def test_copilot_argv_and_skill_dir():
    copilot = Copilot(AgentOptions(extra_args=["--allow-all-tools"]))
    argv = copilot.new_session_argv(session_id="s", name="n", prompt="p", add_dirs=[Path("d")])
    assert argv[1:] == ["--session-id=s", "--name=n", "-i", "p", "--add-dir", "d", "--allow-all-tools"]
    assert copilot.resume_argv("s", add_dirs=[], extra=["--x"])[1:] == ["--resume=s", "--allow-all-tools", "--x"]
    assert copilot.skill_dir == Path.home() / ".agents" / "skills" / "readthrough"
    assert Claude().skill_dir.name == "readthrough" and Claude().skill_dir.parent.name == "skills"


def test_copilot_first_message_is_none_until_written(tmp_path: Path):
    folder = tmp_path / "copilot" / "session-state" / "abc"
    folder.mkdir(parents=True)
    record = SessionRecord("abc", folder, 0.0)
    copilot = Copilot()
    assert copilot.first_user_message(record) is None, "no events yet"
    (folder / "events.jsonl").write_text(
        '{"type":"session.start"}\n{"type":"user.message","data":{"content":"hi', encoding="utf-8"
    )
    assert copilot.first_user_message(record) is None, "a half-written line is read again later"
    (folder / "events.jsonl").write_text(
        '{"type":"session.start"}\n{"type":"user.message","data":{"content":"hi"}}\n', encoding="utf-8"
    )
    assert copilot.first_user_message(record) == "hi"
    assert copilot.session_meta(record) == {}
    assert [r.id for r in copilot.sessions()] == ["abc"]


def test_claude_sessions_are_the_uuid_named_project_files(tmp_path: Path):
    root = tmp_path / "claude" / "projects"
    (root / "c--src-r").mkdir(parents=True)
    sid = "12345678-1234-1234-1234-123456789abc"
    (root / "c--src-r" / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "first words"}}) + "\n", encoding="utf-8"
    )
    (root / "c--src-r" / "notes.jsonl").write_text("{}\n", encoding="utf-8")
    claude = Claude()
    records = list(claude.sessions())
    assert [r.id for r in records] == [sid]
    assert claude.first_user_message(records[0]) == "first words"
    assert claude.session_meta(records[0]) == {"client": "claude"}
    info = claude.session_info(sid)
    assert info.exists and info.active is None and info.updated
    assert (
        not claude.session_info("not-a-uuid").exists
        and not claude.session_info("0" * 8 + "-0000-0000-0000-" + "0" * 12).exists
    )


def test_registry_lists_both_agents():
    assert agents.AGENTS.names() == ["copilot", "claude"], "registration order, which is also the auto order"
    assert [a.name for a in agents.all_agents()] == ["copilot", "claude"]
    for plugin in agents.AGENTS:
        assert plugin.summary and plugin.impl.label and plugin.impl.program and plugin.impl.install_hint
