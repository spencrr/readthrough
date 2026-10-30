import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import PROFILE_PATH, REPO, SLUG

from readthrough import agents, cli, ledger, logs, onboard, review, system
from readthrough.faults import AuthError, NetworkError

CONFIG = f"""
default_repo = "{REPO}"

[[repos]]
name = "{REPO}"
onboarded = "2026-10-01T00:00:00+00:00"

[[repos]]
name = "o/waiting"
"""


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path: Path, make_pr, fake_forge):
    logs.reset()
    opened: list[str] = []
    FakeGitHub = fake_forge  # noqa: N806 - the historical name, kept so the tests read the same
    FakeGitHub.prs = [make_pr(number=1), make_pr(number=2, title="FEAT: other", author={"login": "me"})]
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot"))
    monkeypatch.setattr("readthrough.build.registered_protocol_command", lambda: None)
    monkeypatch.setattr("readthrough.cli.signed_in_accounts", lambda: ["me"])
    monkeypatch.setattr("readthrough.cli.run", lambda cmd, **kw: "origin\n" if "remote" in cmd else "tool 1.0\n")
    monkeypatch.setattr("readthrough.serve.server_alive", lambda port, **kw: None)
    monkeypatch.setattr("readthrough.cli.webbrowser.open", opened.append)
    monkeypatch.setattr(system, "registered_protocol_command", lambda: None)
    monkeypatch.setattr(system, "schedule_status", lambda name=system.TASK_NAME: system.ScheduleStatus(False))
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(CONFIG, encoding="utf-8")
    local_profile = home / "repos" / Path(*SLUG.split("/")) / "profile.toml"
    local_profile.parent.mkdir(parents=True)
    local_profile.write_text(PROFILE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    yield SimpleNamespace(home=home, opened=opened, tmp=tmp_path, profile=local_profile, forge=FakeGitHub)
    logs.reset()


def dashboard(env, *argv: str) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--home", str(env.home), *argv])
    return int(exc.value.code or 0)


def repo_dir(env) -> Path:
    return env.profile.parent


def test_build_writes_the_dashboard_and_briefs(isolated, capsys):
    assert dashboard(isolated, "build", "--no-diffs") == 0
    data = json.loads((repo_dir(isolated) / "data.json").read_text(encoding="utf-8"))
    assert [p["number"] for p in data["prs"]] == [1, 2]
    assert data["stats"]["rate_limit"] == {"graphql": 4900, "rest": 4800}
    assert (repo_dir(isolated) / "index.html").exists()
    err = capsys.readouterr().err
    assert f"{REPO}: 2 open PRs" in err and "Dashboard:" in err
    assert "build started" in (isolated.home / "readthrough.log").read_text(encoding="utf-8")

    assert dashboard(isolated, "brief", "1") == 0
    assert "#1" in capsys.readouterr().out
    assert dashboard(isolated, "brief", "1", "--path") == 0
    assert capsys.readouterr().out.strip().endswith("pr-1.md")
    assert dashboard(isolated, "brief", "99") == 1


def test_build_open_always_opens_the_static_dashboard(isolated):
    assert dashboard(isolated, "build", "--no-diffs", "--open") == 0
    assert isolated.opened and isolated.opened[0].endswith("index.html")
    assert (repo_dir(isolated) / "seen.json").exists()


def test_a_failed_build_names_its_error_id(isolated, capsys):
    isolated.forge.fail = NetworkError("graphql OpenPRs failed: reset", stderr="connection reset by peer")
    assert dashboard(isolated, "build") == 1
    out = capsys.readouterr().out
    assert "the build failed [error " in out and "readthrough.log" in out
    assert "at step 'fetching PRs'" in (isolated.home / "readthrough.log").read_text(encoding="utf-8")


def test_build_refuses_repos_that_arent_onboarded(isolated, capsys):
    assert dashboard(isolated, "--repo", "o/waiting", "build") == 1
    assert "isn't onboarded yet" in capsys.readouterr().err
    (isolated.home / "config.toml").write_text('[[repos]]\nname = "o/waiting"\n', encoding="utf-8")
    assert dashboard(isolated, "build", "--all") == 1
    assert "No onboarded repos" in capsys.readouterr().err


def test_ledger_records_shows_and_clears(isolated, capsys):
    assert dashboard(isolated, "ledger", "1", "--status", "reviewed", "--note", "looked at retries") == 0
    assert "#1: reviewed" in capsys.readouterr().out
    assert dashboard(isolated, "ledger", "1") == 0
    assert "looked at retries" in capsys.readouterr().out
    assert dashboard(isolated, "ledger", "1", "--clear") == 0
    entry = ledger.load(repo_dir(isolated) / "ledger.json")["1"]
    assert "status" not in entry and entry["note"] == "looked at retries"
    assert dashboard(isolated, "ledger", "7") == 0
    assert "No ledger entry for #7" in capsys.readouterr().out


def test_study_adds_entries_and_shows_the_log(isolated, capsys):
    assert dashboard(isolated, "build", "--no-diffs") == 0
    capsys.readouterr()
    assert dashboard(isolated, "study", "1", "--prediction", "touches scoring",
                 "--finding", "major: swallows errors", "--at", "courier/transports/smtp.py:12") == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "added prediction" in out and "added finding" in out
    assert dashboard(isolated, "study", "1") == 0
    out = capsys.readouterr().out
    assert "touches scoring" in out and "swallows errors" in out
    assert "swallows errors" in (repo_dir(isolated) / "briefs" / "pr-1.md").read_text(encoding="utf-8")
    assert dashboard(isolated, "study", "2") == 0
    assert "No study log for #2" in capsys.readouterr().out


def test_config_shows_checks_and_explains_settings(isolated, capsys):
    assert dashboard(isolated, "config") == 0
    out = capsys.readouterr().out
    assert "all defaults" in out and f"[{REPO}]" in out and "[o/waiting]" in out
    assert dashboard(isolated, "config", "--check") == 0
    assert "valid (2 repos)" in capsys.readouterr().out
    assert dashboard(isolated, "config", "--defaults") == 0
    assert "[forge]" in capsys.readouterr().out

    config = isolated.home / "config.toml"
    config.write_text(config.read_text(encoding="utf-8") + "\n[tuning.forge]\nretries = 5\n", encoding="utf-8")
    assert dashboard(isolated, "config") == 0
    assert "forge: retries=5" in capsys.readouterr().out


def test_an_invalid_config_is_reported_without_a_traceback(isolated, capsys):
    (isolated.home / "config.toml").write_text("[tuning.forge]\nretrys = 5\n", encoding="utf-8")
    assert dashboard(isolated, "config", "--check") == 1
    err = capsys.readouterr().err
    assert "retries" in err and "Traceback" not in err


def test_repos_default_and_remove(isolated, capsys, monkeypatch):
    assert dashboard(isolated, "repos") == 0
    out = capsys.readouterr().out
    assert f"* {REPO}" in out and "NOT ONBOARDED" in out
    assert dashboard(isolated, "repos", "--json") == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["server"] == {"running": False, "port": 8765} and listing["default"] == SLUG
    mine = next(r for r in listing["repos"] if r["slug"] == SLUG)
    assert mine["name"] == REPO and mine["onboarded"] and mine["default"] and mine["built"] is None
    assert mine["url"] == f"http://127.0.0.1:8765/r/{SLUG}/"
    assert not next(r for r in listing["repos"] if r["name"] == "o/waiting")["onboarded"]
    # A server on another port than the config's (`serve --port`) is the one the URLs point at.
    monkeypatch.setattr(
        "readthrough.serve.running_server", lambda home, **kw: {"port": 9999, "token": "t", "health": {"repos": [SLUG]}}
    )
    assert dashboard(isolated, "repos", "--json") == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["server"] == {"running": True, "port": 9999, "serving": [SLUG]}
    assert next(r for r in listing["repos"] if r["slug"] == SLUG)["url"] == f"http://127.0.0.1:9999/r/{SLUG}/"
    assert dashboard(isolated, "default", "o/waiting") == 0
    assert dashboard(isolated, "repos") == 0
    assert "* o/waiting" in capsys.readouterr().out
    assert dashboard(isolated, "default", "o/unknown") == 1

    assert dashboard(isolated, "--repo", REPO, "build", "--no-diffs") == 0
    assert dashboard(isolated, "remove", REPO, "--purge") == 0
    assert not repo_dir(isolated).exists()
    assert dashboard(isolated, "remove", "o/waiting") == 0
    assert "still in" in capsys.readouterr().out
    assert dashboard(isolated, "remove", "o/unknown") == 1
    assert dashboard(isolated, "repos") == 0
    assert "No repos yet" in capsys.readouterr().out


def test_show_tells_the_truth_about_who_is_listening(isolated, monkeypatch, capsys):
    """`show` steers open pages through the live server; without a server, or a page, it says so and fails."""
    import urllib.request

    found: dict | None = None
    posted: list[dict] = []
    delivered = {"n": 0}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({"ok": True, "delivered": delivered["n"]}).encode()

    def urlopen(request, timeout):  # the signature urllib has
        posted.append(
            {"url": request.full_url, "token": request.get_header("X-readthrough-token"), **json.loads(request.data)}
        )
        return Response()

    monkeypatch.setattr("readthrough.serve.running_server", lambda home, **kw: found)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    assert dashboard(isolated, "show", "2") == 1
    assert "isn't running" in capsys.readouterr().err
    found = {"port": 8765, "token": "t0k", "instance": "abc", "health": {"repos": ["github/elsewhere/other"]}}
    assert dashboard(isolated, "show", "2") == 1
    assert "isn't serving" in capsys.readouterr().err
    found["health"]["repos"] = [SLUG]
    assert dashboard(isolated, "show", "2", "--client", "canvas-1", "--wait", "0.4") == 1
    err = capsys.readouterr().err
    assert "No dashboard page is showing" in err and "as canvas-1" in err
    assert len(posted) >= 2, "it kept trying for as long as it was told to"
    assert posted[0] == {
        "url": f"http://127.0.0.1:8765/r/{SLUG}/api/focus",
        "token": "t0k",
        "pr": 2,
        "panel": "overview",
        "client": "canvas-1",
    }
    delivered["n"] = 2
    assert dashboard(isolated, "show", "2", "--panel", "none") == 0
    assert posted[-1]["panel"] == "none" and "client" not in posted[-1]
    assert dashboard(isolated, "show", "0") == 1
    assert dashboard(isolated, "show", "2", "--client", "no spaces") == 1


def test_profile_actions(isolated, capsys):
    assert dashboard(isolated, "profile") == 0
    assert "Layers:" in capsys.readouterr().out
    assert dashboard(isolated, "profile", "sources") == 0
    assert capsys.readouterr().out.startswith("-> local")
    assert dashboard(isolated, "profile", "path") == 0
    shown = capsys.readouterr().out.strip()
    assert Path(shown).expanduser().resolve() == isolated.profile.resolve(), (
        "the path, written ~-relative when it can be"
    )
    assert dashboard(isolated, "profile", "export") == 0
    assert f"Architecture profile for {REPO}" in capsys.readouterr().out
    assert dashboard(isolated, "--repo", "o/waiting", "profile") == 1


def test_profile_check_validates_a_file(isolated, capsys, tmp_path: Path):
    assert dashboard(isolated, "profile", "check", "--file", str(PROFILE_PATH)) == 0
    assert f"a valid profile for {REPO}" in capsys.readouterr().out
    bad = tmp_path / "bad.toml"
    bad.write_text('schema = 1\nrepo = "o/r"\n', encoding="utf-8")
    assert dashboard(isolated, "profile", "check", "--file", str(bad)) == 1


def test_onboarding_drafts_then_accepts(isolated, capsys, monkeypatch):
    from test_onboard import AUTHORS, CHURN, TREE

    facts = onboard.RepoFacts("o/app", "main", TREE, CHURN, AUTHORS)
    monkeypatch.setattr(onboard, "gather", lambda forge, **kw: facts)
    assert dashboard(isolated, "onboard", "o/app", "--clone", str(isolated.tmp / "nowhere")) == 0
    profile = isolated.home / "repos" / "github" / "o" / "app" / "profile.toml"
    assert profile.exists()
    assert "--accept" in capsys.readouterr().out

    assert dashboard(isolated, "onboard", "o/app", "--draft") == 0
    assert "Keeping your draft" in capsys.readouterr().err
    assert dashboard(isolated, "onboard", "o/app", "--update") == 0
    assert list(profile.parent.glob("profile.*-*.toml")), "the previous draft is backed up"

    assert dashboard(isolated, "onboard", "o/app", "--accept") == 0
    assert "o/app is onboarded" in capsys.readouterr().out
    assert dashboard(isolated, "repos") == 0
    assert "o/app" in capsys.readouterr().out


def test_onboarding_an_unreadable_repo_lists_your_accounts(isolated, capsys):
    isolated.forge.unreadable = AuthError("api repos/o/secret failed", stderr="HTTP 403")
    assert dashboard(isolated, "onboard", "o/secret") == 1
    assert "Signed-in gh accounts: me" in capsys.readouterr().err


def test_doctor_reports_problems_and_writes_a_bundle(isolated, capsys, monkeypatch):
    monkeypatch.delenv("READTHROUGH_COPILOT")
    monkeypatch.delenv("READTHROUGH_CLAUDE")
    monkeypatch.setattr("readthrough.process.shutil.which", lambda name: None)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert dashboard(isolated, "build", "--no-diffs") == 0
    capsys.readouterr()
    assert dashboard(isolated, "doctor") == 1
    out = capsys.readouterr().out
    assert "XX  agent" in out and "copilot isn't installed" in out and "ok  access" in out and "ok  onboarded" in out
    assert "readthrough install skill --agent copilot" in out

    assert dashboard(isolated, "doctor", "--bundle") == 0
    bundle = next((isolated.home / "bundles").glob("readthrough-debug-*.zip"))
    with zipfile.ZipFile(bundle) as z:
        names = set(z.namelist())
        versions = json.loads(z.read("versions.json"))
    assert {"versions.json", "config.toml", "health.json", "readthrough.log"} <= names
    assert f"repos/{SLUG.replace('/', '__')}.json" in names
    assert versions["gh_accounts"] == ["me"] and versions["repos"][0]["built"]


def test_doctor_shows_the_servers_last_failure(isolated, capsys, monkeypatch):
    status = {SLUG: {"error": {"id": "abc123", "message": "reset", "step": "fetching PRs", "at": "10:00"},
                     "consecutive_failures": 3}}  # fmt: skip
    monkeypatch.setattr("readthrough.serve.server_alive", lambda port, **kw: {"repos": [SLUG], "status": status})
    assert dashboard(isolated, "--repo", REPO, "doctor") == 1
    out = capsys.readouterr().out
    assert "3 failed refresh(es) in a row" in out and "error abc123" in out


def test_skill_install_and_uninstall(isolated, capsys, monkeypatch):
    dest = isolated.tmp / "skill"
    assert dashboard(isolated, "install", "skill", "--dest", str(dest)) == 0
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == agents.bundled_skill()
    assert dashboard(isolated, "uninstall", "skill", "--dest", str(dest)) == 0
    assert capsys.readouterr().out.strip().endswith("removed")
    assert not dest.exists()

    # Without --dest the skill goes to every installed agent: Copilot's ~/.agents/skills and Claude Code's skills.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: isolated.tmp / "homedir"))
    assert dashboard(isolated, "install", "skill") == 0
    out = capsys.readouterr().out
    assert (isolated.tmp / "homedir" / ".agents" / "skills" / "readthrough" / "SKILL.md").exists()
    assert (isolated.tmp / "claude" / "skills" / "readthrough" / "SKILL.md").exists()
    assert out.count("Installed") == 2
    assert dashboard(isolated, "uninstall", "skill", "--agent", "claude") == 0
    assert not (isolated.tmp / "claude" / "skills" / "readthrough").exists()
    assert (isolated.tmp / "homedir" / ".agents" / "skills" / "readthrough" / "SKILL.md").exists()


def test_install_with_nothing_named_offers_what_is_missing(isolated, capsys, monkeypatch):
    """`readthrough install` asks about each piece that isn't set up (the nudge defaults to no), `--all` does the
    recommended ones without asking, and a non-terminal without `--all` gets the plan and a non-zero exit."""
    calls: list = []
    monkeypatch.setattr(system, "install_protocol", lambda: calls.append("protocol") or "handler")
    monkeypatch.setattr(system, "install_schedule", lambda **kw: calls.append("schedule") or "cmd")
    monkeypatch.setattr(system, "install_server_task", lambda **kw: calls.append("server") or "cmd")
    monkeypatch.setattr(system, "automated_here", lambda: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: isolated.tmp / "homedir"))

    # Not a terminal: say what would be done and how, and don't do it.
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert dashboard(isolated, "install") == 1
    out = capsys.readouterr().out
    assert "Not set up yet:" in out and "skill (copilot)" in out and "protocol" in out and "install --all" in out
    assert calls == []

    # A terminal: one question per piece (both agents count as installed here, and the Copilot home exists, so
    # the canvas is offered); the answers in order are yes, no, yes, no, Enter (the default: yes), Enter (the
    # nudge's default: no).
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    answers = iter(["y", "n", "y", "n", "", ""])
    asked: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or next(answers))
    assert dashboard(isolated, "install") == 0
    out = capsys.readouterr().out
    assert len(asked) == 6, asked  # skill (copilot), skill (claude), canvas, protocol, server, schedule
    assert asked[:5] == ["Set it up? [Y/n] "] * 5 and asked[5] == "Set it up? [y/N] ", "the nudge defaults to no"
    assert (isolated.tmp / "homedir" / ".agents" / "skills" / "readthrough" / "SKILL.md").exists()
    assert not (isolated.tmp / "claude" / "skills" / "readthrough").exists(), "declined"
    assert (isolated.tmp / "copilot" / "extensions" / "readthrough" / "extension.mjs").exists()
    assert calls == ["server"], "protocol was declined; the nudge's default is no"
    assert "Set up: skill (copilot), canvas, server" in out

    # --all: the recommended pieces, no questions; the nudge stays a choice.
    calls.clear()
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("--all must not ask"))
    assert dashboard(isolated, "install", "--all") == 0
    assert calls == ["protocol", "server"], "the copilot skill is current now, and the nudge isn't recommended"
    assert (isolated.tmp / "claude" / "skills" / "readthrough" / "SKILL.md").exists()
    assert "Set up: skill (claude), protocol, server" in capsys.readouterr().out


def test_install_with_everything_in_place_says_so(isolated, capsys, monkeypatch):
    monkeypatch.setattr(system, "automated_here", lambda: False)  # nothing to automate but the skill and the canvas
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: isolated.tmp / "homedir"))
    assert dashboard(isolated, "install", "skill") == 0
    assert dashboard(isolated, "install", "canvas") == 0
    assert dashboard(isolated, "install") == 0
    out = capsys.readouterr().out
    assert "Everything readthrough can set up here is set up" in out


def test_doctor_points_at_the_one_command_when_two_or_more_pieces_are_missing(isolated, capsys, monkeypatch):
    monkeypatch.setattr(system, "automated_here", lambda: True)
    assert dashboard(isolated, "doctor") in (0, 1)
    out = capsys.readouterr().out
    assert "XX  skill (copilot)" in out and "XX  protocol" in out
    assert "--  server         not running" in out, "a server that isn't running is something to want, not a fault"
    assert "--  schedule" in out, "the nudge is a choice, not a problem"
    assert "`readthrough install` sets these up in one go" in out


def test_canvas_install_records_how_to_run_readthrough_and_uninstall_takes_only_its_own(isolated, capsys, monkeypatch):
    """The canvas goes under the Copilot home's extensions with a record of the interpreter that installed it;
    the doctor tells a current install from a stale one; uninstall removes exactly what install wrote."""
    dest = isolated.tmp / "copilot" / "extensions" / "readthrough"
    assert dashboard(isolated, "install", "canvas") == 0
    assert "experimental" in capsys.readouterr().out
    assert {p.name for p in dest.iterdir()} == {"extension.mjs", "readthrough.mjs", "readthrough.json"}
    shipped = Path(cli.__file__).parent / "assets" / "extension"
    assert (dest / "extension.mjs").read_text(encoding="utf-8") == (shipped / "extension.mjs").read_text(
        encoding="utf-8"
    )
    record = json.loads((dest / "readthrough.json").read_text(encoding="utf-8"))
    assert record["argv"] == [sys.executable, "-m", "readthrough"] and record["version"] == cli.__version__
    assert system.canvas_state(dest)[0] is True

    # Doctor: installed and current. A record from another install is reported as such, not as fine.
    monkeypatch.setattr("readthrough.cli.signed_in_accounts", lambda: ["me"])
    assert dashboard(isolated, "doctor") in (0, 1)
    assert "ok  canvas" in capsys.readouterr().out
    (dest / "readthrough.json").write_text(
        json.dumps({"argv": ["C:/old/python.exe", "-m", "readthrough"], "version": "0.1.0"}), encoding="utf-8"
    )
    ok, detail = system.canvas_state(dest)
    assert ok is False and "0.1.0" in detail and "C:/old/python.exe" in detail
    (dest / "readthrough.json").write_text("{not json", encoding="utf-8")
    assert system.canvas_state(dest) == (False, f"{dest} (its record is unreadable)")

    (dest / "theirs.txt").write_text("not ours", encoding="utf-8")
    assert dashboard(isolated, "uninstall", "canvas") == 0
    assert capsys.readouterr().out.strip().endswith("removed")
    assert [p.name for p in dest.iterdir()] == ["theirs.txt"], "a file that isn't ours stays, and so does its directory"
    assert system.canvas_state(dest) == (None, "not installed")
    assert dashboard(isolated, "uninstall", "canvas") == 0
    assert "nothing to remove" in capsys.readouterr().out

    other = isolated.tmp / "elsewhere"
    assert dashboard(isolated, "install", "canvas", "--dest", str(other)) == 0
    assert (other / "extension.mjs").exists()
    assert dashboard(isolated, "uninstall", "canvas", "--dest", str(other)) == 0
    assert not other.exists(), "an empty directory of ours goes too"


def test_system_installs_are_delegated(isolated, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(system, "install_protocol", lambda: calls.append("protocol") or "handler")
    monkeypatch.setattr(system, "install_schedule", lambda **kw: calls.append(("schedule", kw)) or "cmd")
    monkeypatch.setattr(system, "install_server_task", lambda **kw: calls.append(("server", kw)) or "cmd")
    monkeypatch.setattr(system, "uninstall_protocol", lambda: False)
    monkeypatch.setattr(system, "uninstall_schedule", lambda name=system.TASK_NAME: calls.append(("rm", name)) or True)
    assert dashboard(isolated, "install", "protocol") == 0
    assert dashboard(isolated, "install", "schedule", "--at", "07:00", "--days", "daily") == 0
    assert dashboard(isolated, "install", "server") == 0
    assert dashboard(isolated, "uninstall", "protocol") == 0
    assert "nothing to remove" in capsys.readouterr().out
    assert dashboard(isolated, "uninstall", "server") == 0
    assert dashboard(isolated, "uninstall", "schedule") == 0
    assert calls[1] == ("schedule", {"at": "07:00", "days": "daily", "open_mode": "auto", "home": isolated.home})
    assert calls[-2:] == [("rm", system.SERVER_TASK_NAME), ("rm", system.TASK_NAME)]


def test_serve_applies_command_line_options_to_reloads_too(isolated, monkeypatch):
    seen = {}

    def fake_serve(settings, *, with_diffs, settings_loader, on_ready):
        seen.update(port=settings.port, minutes=settings.refresh_minutes, reloaded=settings_loader(), diffs=with_diffs)

    monkeypatch.setattr("readthrough.serve.serve", fake_serve)
    assert dashboard(isolated, "serve", "--port", "9123", "--interval", "3", "--no-diffs") == 0
    assert (seen["port"], seen["minutes"], seen["diffs"]) == (9123, 3, False)
    assert (seen["reloaded"].port, seen["reloaded"].refresh_minutes) == (9123, 3)


def test_serve_and_open_use_a_running_server(isolated, monkeypatch):
    monkeypatch.setattr("readthrough.serve.server_alive", lambda port, **kw: {"repos": [REPO]})
    assert dashboard(isolated, "serve", "--open") == 0
    assert dashboard(isolated, "open") == 0
    assert all(url.startswith("http://127.0.0.1:") for url in isolated.opened) and len(isolated.opened) == 2


def test_open_without_a_dashboard_says_how_to_make_one(isolated, capsys):
    assert dashboard(isolated, "open") == 1
    assert "readthrough build" in capsys.readouterr().err


def test_review_prints_or_opens_the_session(isolated, capsys, monkeypatch):
    assert dashboard(isolated, "review", "1", "--print") == 1
    assert "No local clone" in capsys.readouterr().err
    clone = isolated.tmp / "clone"
    clone.mkdir()
    config = isolated.home / "config.toml"
    text = config.read_text(encoding="utf-8").replace(
        f'name = "{REPO}"', f'name = "{REPO}"\nclone = "{clone.as_posix()}"'
    )
    config.write_text(text, encoding="utf-8")
    assert dashboard(isolated, "review", "1", "--print") == 0
    assert "copilot" in capsys.readouterr().out
    assert dashboard(isolated, "review", "1", "--vscode", "--fresh") == 0
    assert isolated.opened[-1].startswith("vscode")
    launched = []
    monkeypatch.setattr(review, "launch_in_terminal", lambda launch, cfg: launched.append(launch) or "wt")
    assert dashboard(isolated, "review", "1") == 0
    recorded = ledger.load(repo_dir(isolated) / "ledger.json")["1"]["session"]
    assert [launch.session_id for launch in launched] == [recorded]


def test_worktree_commands(isolated, capsys, monkeypatch):
    monkeypatch.setattr(review, "ensure_worktree", lambda cfg, n, base: (Path("wt"), "base1", "head1"))
    monkeypatch.setattr(review, "remove_worktree", lambda cfg, n: False)
    assert dashboard(isolated, "worktree", "1") == 0
    assert "merge-base base1" in capsys.readouterr().out
    assert dashboard(isolated, "worktree", "1", "--remove") == 0
    assert "no worktree" in capsys.readouterr().out


def test_url_handler_rejects_bad_links_in_a_message_box(isolated, monkeypatch):
    boxes = []
    monkeypatch.setattr(system, "message_box", lambda title, text: boxes.append(text))
    assert dashboard(isolated, "handle-url", "readthrough://review/evil/repo/1") == 1
    assert boxes and "readthrough.log" in boxes[0]


def test_url_handler_routes_actions(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_review", lambda s, ns: calls.append(("review", ns.pr)) or 0)
    monkeypatch.setattr(cli, "cmd_build", lambda s, ns: calls.append(("refresh", ns.repo)) or 0)
    monkeypatch.setattr(cli, "cmd_open", lambda s, ns: calls.append(("open", ns.repo)) or 0)
    for url in (
        f"readthrough://review/{SLUG}/1",
        f"readthrough://refresh/{SLUG}",
        f"readthrough://open/{REPO}",  # the pre-forge form still names a GitHub repo
        "readthrough://open",
    ):
        assert dashboard(isolated, "handle-url", url) == 0
    assert calls == [("review", 1), ("refresh", SLUG), ("open", SLUG), ("open", None)]


def test_unexpected_errors_get_an_id_and_point_at_the_bundle(isolated, capsys, monkeypatch):
    def broken(settings, args):
        raise KeyError("oops")

    monkeypatch.setattr(cli, "cmd_repos", broken)
    assert dashboard(isolated, "repos") == 2
    err = capsys.readouterr().err
    assert "unexpected error (KeyError) [error " in err and "doctor --bundle" in err
    assert "Traceback" in (isolated.home / "readthrough.log").read_text(encoding="utf-8")


def test_expected_errors_are_one_line(isolated, capsys, monkeypatch):
    def failing(settings, args):
        raise NetworkError("api failed", stderr="connection reset")

    monkeypatch.setattr(cli, "cmd_repos", failing)
    assert dashboard(isolated, "repos") == 1
    assert "Traceback" not in capsys.readouterr().err
