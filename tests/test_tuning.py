import json
import logging
import tomllib
from dataclasses import fields
from pathlib import Path

import pytest

from readthrough import logs
from readthrough.config import ConfigError, Settings, load_settings
from readthrough.tuning import REPO_SECTIONS, Tuning, TuningError, changed_from_defaults, defaults_text, load_tuning


def test_defaults_file_and_code_agree_and_every_setting_is_explained():
    raw = tomllib.loads(defaults_text())
    lines = defaults_text().splitlines()
    for section in fields(Tuning):
        declared = {f.name for f in fields(getattr(load_tuning(), section.name))}
        assert set(raw[section.name]) == declared, section.name
        for key in declared:
            at = next(i for i, line in enumerate(lines) if line.startswith(f"{key} ="))
            comment = next(
                (
                    lines[j]
                    for j in range(at - 1, -1, -1)
                    if not lines[j].startswith(tuple("abcdefghijklmnopqrstuvwxyz"))
                ),
                "",
            )
            assert comment.startswith("#"), f"{section.name}.{key} has no explanation above it"
    assert set(raw) == {f.name for f in fields(Tuning)}
    for name in REPO_SECTIONS:
        assert f"[{name}]  # per repo" in defaults_text()


def test_overrides_are_validated_with_helpful_messages():
    with pytest.raises(TuningError, match="did you mean 'timeout_seconds'"):
        load_tuning({"forge": {"timout_seconds": 5}})
    with pytest.raises(TuningError, match="between 0 and 10"):
        load_tuning({"forge": {"retries": 99}})
    with pytest.raises(TuningError, match="whole number"):
        load_tuning({"forge": {"retries": 1.5}})
    with pytest.raises(TuningError, match="can't be set per repo"):
        load_tuning(repo_overrides={"forge": {"retries": 1}})
    with pytest.raises(TuningError, match="non-empty list"):
        load_tuning({"server": {"retry_after_failure_minutes": []}})
    assert load_tuning({"logging": {"level": "DEBUG"}}).logging.level == "debug"


CONFIG = """
schema = 2
default_repo = "o/r"

[tuning.forge]
timeout_seconds = 90

[tuning.planning]
estimate_max_minutes = 120

[[repos]]
name = "o/r"
clone = "~/src/r"

[repos.tuning.planning]
estimate_max_minutes = 60

[[repos]]
name = "o/s"
"""


def test_config_tuning_layers_and_round_trips(tmp_path: Path):
    (tmp_path / "config.toml").write_text(CONFIG, encoding="utf-8")
    settings = load_settings(tmp_path)
    r, s = settings.config_for("o/r"), settings.config_for("o/s")
    assert (r.tuning.forge.timeout_seconds, r.tuning.planning.estimate_max_minutes) == (90, 60)
    assert s.tuning.planning.estimate_max_minutes == 120
    assert r.clone == Path.home() / "src" / "r"
    assert changed_from_defaults(settings.tuning) == {
        "forge": {"timeout_seconds": 90.0},
        "planning": {"estimate_max_minutes": 120},
    }
    settings.save()
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert 'clone = "~/src/r"' in text and "[repos.tuning.planning]" in text and "schema = 2" in text
    again = load_settings(tmp_path)
    assert again.config_for("o/r").tuning.planning.estimate_max_minutes == 60


def test_config_errors_point_at_the_problem(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[tuning.forge]\nretries = "lots"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"tuning\.forge\.retries must be a whole number"):
        load_settings(tmp_path)
    path.write_text('[[repos]]\nname = "o/r"\nclnoe = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="did you mean 'clone'"):
        load_settings(tmp_path)
    path.write_text("schema = 99\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="schema 99"):
        load_settings(tmp_path)


def test_config_can_live_elsewhere(tmp_path: Path, monkeypatch):
    elsewhere = tmp_path / "dotfiles" / "readthrough.toml"
    elsewhere.parent.mkdir()
    elsewhere.write_text('default_repo = "o/r"\n[[repos]]\nname = "o/r"\n', encoding="utf-8")
    monkeypatch.setenv("READTHROUGH_CONFIG", str(elsewhere))
    monkeypatch.setattr("readthrough.config.DEFAULT_HOME", tmp_path / "home")
    settings = load_settings()
    assert settings.path == elsewhere and settings.default_repo == "o/r"
    assert settings.config_for().repo_dir == tmp_path / "home" / "repos" / "github" / "o" / "r"
    assert load_settings(tmp_path / "explicit").path == tmp_path / "explicit" / "config.toml"


# ---------------------------------------------------------------- logging


@pytest.fixture
def fresh_logging():
    logs.reset()
    yield
    logs.reset()


def _logger(tmp_path: Path, fmt: str = "text") -> tuple[logging.Logger, Path]:
    path = tmp_path / "readthrough.log"
    logs.setup(path, load_tuning({"logging": {"format": fmt}}).logging, console="none")
    return logging.getLogger("readthrough"), path


def test_log_lines_carry_repo_and_build_even_in_worker_threads(tmp_path: Path, fresh_logging):
    log, path = _logger(tmp_path)
    with logs.context("o/r", "b7f3"):
        log.info("main thread")
        logs.pool_map(lambda n: logging.getLogger("readthrough.github").info("worker %d", n), [1, 2], 2)
    log.info("outside")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert all("[o/r b7f3] " in line for line in lines[:3])
    assert lines[3].endswith(" outside") and "[" not in lines[3].split("INFO", 1)[1]
    assert lines[0][:4].isdigit() and " INFO " in lines[0]


def test_json_logs_are_one_object_per_line(tmp_path: Path, fresh_logging):
    log, path = _logger(tmp_path, "json")
    with logs.context("o/r", "abcd"):
        log.warning("build failed", extra={"error_id": "e1", "step": "fetching PRs"})
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["repo"] == "o/r" and rec["build"] == "abcd" and rec["error_id"] == "e1" and rec["level"] == "warning"


def test_setup_runs_once_until_reset(tmp_path: Path, fresh_logging):
    _logger(tmp_path)
    handlers = logging.getLogger("readthrough").handlers[:]
    _logger(tmp_path)
    assert logging.getLogger("readthrough").handlers == handlers
    logs.reset()
    assert logging.getLogger("readthrough").handlers == []


def test_a_github_owner_named_like_a_forge_is_not_a_slug(tmp_path: Path):
    settings = Settings(home=tmp_path)
    cfg = settings.config_for("gitlab/gitlab-foss")  # the GitHub mirror organization, not a GitLab slug
    assert (cfg.forge, cfg.repo, cfg.slug) == ("github", "gitlab/gitlab-foss", "github/gitlab/gitlab-foss")
    cfg = settings.config_for("gitlab/gitlab-org/cli")
    assert (cfg.forge, cfg.repo) == ("gitlab", "gitlab-org/cli")
    cfg = settings.config_for("forgejo/forgejo")  # a plain owner/name; the forge must be said
    assert cfg.forge == "github"
    assert settings.config_for("forgejo/forgejo", forge="forgejo").slug == "forgejo/forgejo/forgejo"
    assert settings.config_for("ado/org/proj/repo").slug == "ado/org/proj/repo"
