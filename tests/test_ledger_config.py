from pathlib import Path

import pytest

from readthrough import ledger
from readthrough.config import ConfigError, load_settings


def test_ledger_round_trip(tmp_path: Path):
    path = tmp_path / "ledger.json"
    ledger.record(path, 5, status="understood", head="abc")
    ledger.record(path, 5, note="learned X")
    entry = ledger.load(path)["5"]
    assert entry["status"] == "understood"
    assert entry["head"] == "abc"
    assert entry["note"] == "learned X"


def test_ledger_rejects_unknown_status(tmp_path: Path):
    with pytest.raises(ValueError):
        ledger.record(tmp_path / "l.json", 1, status="lgtm")


CONFIG = """
default_repo = "o/r"
port = 9000

[defaults]
session_minutes = 45

[[repos]]
name = "o/r"
clone = "~/x"
onboarded = "2026-10-01T00:00:00+00:00"

[[repos]]
name = "corp/private"
account = "me_corp"
session_minutes = 30
"""


def test_settings_file(tmp_path: Path):
    (tmp_path / "config.toml").write_text(CONFIG, encoding="utf-8")
    settings = load_settings(tmp_path)
    cfg = settings.config_for()
    assert (cfg.repo, cfg.clone, cfg.session_minutes, cfg.port) == ("o/r", Path.home() / "x", 45, 9000)
    assert cfg.repo_dir == tmp_path / "repos" / "github" / "o" / "r"
    corp = settings.config_for("CORP/Private")
    assert (corp.repo, corp.account, corp.session_minutes, corp.onboarded) == ("corp/private", "me_corp", 30, None)


def test_settings_round_trip(tmp_path: Path):
    (tmp_path / "config.toml").write_text(CONFIG, encoding="utf-8")
    settings = load_settings(tmp_path)
    settings.upsert("new/repo", clone=tmp_path / "c", internal_authors=["a", "b"])
    settings.remove("o/r")
    settings.save()
    again = load_settings(tmp_path)
    assert again.names() == ["corp/private", "new/repo"]
    assert again.default_repo is None
    assert again.config_for("new/repo").internal_authors == ["a", "b"]
    assert again.config_for("new/repo").clone == tmp_path / "c"
    assert again.defaults == {"session_minutes": 45}


def test_settings_reject_unknown_keys(tmp_path: Path):
    (tmp_path / "config.toml").write_text('repoo = "typo"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="repoo"):
        load_settings(tmp_path)
    (tmp_path / "config.toml").write_text('[[repos]]\nname = "o/r"\nclon = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="clon"):
        load_settings(tmp_path)
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "missing").upsert("o/r", nonsense=1)


def test_settings_need_a_repo_when_ambiguous(tmp_path: Path):
    settings = load_settings(tmp_path)
    with pytest.raises(ConfigError, match="onboard"):
        settings.config_for()
    settings.upsert("a/b")
    settings.upsert("c/d")
    with pytest.raises(ConfigError, match="--repo"):
        settings.config_for()
    assert settings.config_for("x/y").repo == "x/y"
    with pytest.raises(ConfigError):
        settings.config_for("not-a-repo")


def test_clear_status_keeps_note_and_session(tmp_path: Path):
    path = tmp_path / "ledger.json"
    ledger.record(path, 3, status="reviewed", note="n", session="s", head="h")
    entry = ledger.clear_status(path, 3)
    assert entry == {"note": "n", "session": "s"}
    ledger.record(path, 4, status="understood")
    ledger.clear_status(path, 4)
    assert "4" not in ledger.load(path)


def test_mark_seen_is_a_whats_new_baseline(tmp_path: Path):
    from readthrough.analyze import whats_new
    from readthrough.build import _load_json, mark_seen

    cfg = load_settings(tmp_path).config_for("o/r")
    pr = {"number": 1, "title": "t", "author": "a", "head": "x", "flags": [], "files": []}
    mark_seen(cfg, {"generated": "g1", "prs": [pr]})
    seen = _load_json(cfg.seen_path)
    news = whats_new(seen, [pr, {**pr, "number": 2}], {}, "me")
    assert news["since"] == "g1"
    assert news["opened"] == [2]


def test_remote_for_asks_the_forge_what_its_urls_look_like(monkeypatch, tmp_path: Path):
    from readthrough import config

    remotes = {
        tmp_path
        / "courier-ops": "origin\thttps://contoso@dev.azure.com/contoso/Delivery%20Team/_git/Courier-ops (fetch)\n",
        tmp_path
        / "vscode": "upstream\tgit@github.com:microsoft/vscode.git (fetch)\norigin\tgit@github.com:me/vscode.git (fetch)\n",
    }
    monkeypatch.setattr(config, "_git", lambda args: remotes.get(Path(args[1]), ""))
    assert config.remote_for(tmp_path / "courier-ops", "contoso/Delivery Team/Courier-Ops", "ado") == "origin"
    assert config.remote_for(tmp_path / "courier-ops", "contoso/Delivery Team/Courier-Ops") is None, "not a GitHub URL"
    assert config.remote_for(tmp_path / "vscode", "microsoft/vscode") == "upstream"
    assert config.remote_for(tmp_path / "vscode", "me/vscode", "github") == "origin"
    assert config.remote_for(tmp_path / "vscode", "someone/vscode") is None
