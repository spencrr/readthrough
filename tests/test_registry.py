"""The plugin registries: validated options, did-you-mean errors, and the config keys that pick plugins."""

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import PROFILE_PATH, REPO

from readthrough import terminals
from readthrough.config import ConfigError, load_settings
from readthrough.profile import PRECEDENCE, PROFILE_SOURCES, ProfileError, profile_sources, shared_profile_path
from readthrough.registry import NoOptions, Registry, RegistryError
from readthrough.terminals import TERMINALS, Command, Launcher, Spawn


@dataclass(frozen=True)
class Options:
    size: int = 3
    name: str = "x"
    ratio: float = 0.5
    on: bool = False
    paths: list[Path] = field(default_factory=list)
    label: str | None = None
    where: Path = Path("here")


@pytest.fixture
def registry() -> Registry[str]:
    reg: Registry[str] = Registry("widget")
    reg.add("plain", "no options")("plain-impl")
    reg.add("fancy", "all the option types", options=Options)("fancy-impl")
    return reg


def test_registry_lookup_and_suggestions(registry: Registry[str]):
    assert registry.names() == ["plain", "fancy"]
    assert [p.name for p in registry] == ["plain", "fancy"]
    assert "plain" in registry and "nope" not in registry
    assert registry.get("fancy").impl == "fancy-impl" and registry.get("plain").options is NoOptions
    with pytest.raises(RegistryError, match="unknown widget 'fancu' \\(did you mean 'fancy'\\?\\)"):
        registry.get("fancu")
    with pytest.raises(RegistryError, match="registered twice"):
        registry.add("plain", "again")("x")
    with pytest.raises(TypeError, match="dataclass"):
        registry.add("bad", "options aren't a dataclass", options=dict)


def test_options_are_validated_and_defaulted(registry: Registry[str], tmp_path: Path):
    opts = registry.options_for(
        "fancy",
        {"size": 4, "ratio": 1, "on": True, "paths": ["a", "~/b"], "label": "L", "where": "sub/dir"},
        where="[widgets.fancy]",
        base=tmp_path,
    )
    assert (opts.size, opts.ratio, opts.on, opts.label, opts.name) == (4, 1.0, True, "L", "x")
    assert opts.paths == [tmp_path / "a", Path.home() / "b"] and opts.where == tmp_path / "sub" / "dir"
    assert registry.defaults_for("fancy") == Options()
    assert registry.options_for("plain", None, where="w") == NoOptions()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"siz": 1}, "unknown option 'siz' for widget 'fancy' \\(did you mean 'size'\\?\\)"),
        ({"size": "big"}, "size must be a whole number"),
        ({"size": True}, "size must be a whole number"),
        ({"ratio": "half"}, "ratio must be a number"),
        ({"on": "yes"}, "on must be true or false"),
        ({"name": 3}, "name must be text"),
        ({"paths": "a"}, "paths must be a list"),
        ({"paths": [1]}, "paths\\[0\\] must be a path"),
        ({"where": ""}, "where must be a path"),
    ],
)
def test_option_errors_are_readable(registry: Registry[str], raw, message):
    with pytest.raises(RegistryError, match=message):
        registry.options_for("fancy", raw, where="[widgets.fancy]")


def test_a_required_option_is_named_when_missing():
    @dataclass(frozen=True)
    class Needs:
        token: str
        extra: int = 1

    reg: Registry[str] = Registry("thing")
    reg.add("strict", "needs a token", options=Needs)("impl")
    with pytest.raises(RegistryError, match="thing 'strict' needs token"):
        reg.options_for("strict", {}, where="[things.strict]")
    assert reg.options_for("strict", {"token": "t"}, where="w").token == "t"


# ---------------------------------------------------------------- terminals

CMD = Command(
    argv=["copilot", "--resume=abc;def", "-i", "hi there"],
    cwd=Path("C:/work" if sys.platform == "win32" else "/work"),
    title="r #1",
)


def test_windows_terminal_escapes_semicolons_and_applies_options(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(terminals.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    wt = TERMINALS.get("wt").impl
    spawn = terminals.plan("wt", wt, CMD)
    assert spawn.argv[:2] == ["C:/bin/wt.exe", "-w"] and spawn.argv[2] == "new"
    assert spawn.argv[3:8] == ["new-tab", "--title", "r #1", "-d", str(CMD.cwd)]
    assert spawn.argv[-3:] == ["--resume=abc\\;def", "-i", "hi there"]
    opts = TERMINALS.options_for(
        "wt", {"profile": "PowerShell", "window": "0", "extra_args": ["--suppressApplicationTitle"]}, where="w"
    )
    spawn = terminals.plan("wt", wt, CMD, {"wt": opts})
    assert "-p" in spawn.argv and spawn.argv[spawn.argv.index("-p") + 1] == "PowerShell"
    assert spawn.argv[2] == "0" and "--suppressApplicationTitle" in spawn.argv
    with pytest.raises(RegistryError, match="did you mean 'profile'"):
        TERMINALS.options_for("wt", {"profil": "x"}, where="[terminals.wt]")


def test_other_launchers_build_their_documented_invocations(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(terminals.shutil, "which", lambda name: None)
    console = terminals.plan("console", TERMINALS.get("console").impl, CMD)
    assert console.argv == CMD.argv and console.cwd == CMD.cwd
    mac = terminals.plan(
        "terminal-app", Launcher("osascript", TERMINALS.get("terminal-app").impl.spawn, ("darwin",)), CMD
    )
    assert mac.argv[0] == "" and mac.argv[1] == "-e" and 'tell application "Terminal" to do script' in mac.argv[2]
    assert "hi there" in mac.argv[2] and "activate" in mac.argv[-1]
    gnome = terminals.plan("gnome-terminal", TERMINALS.get("gnome-terminal").impl, CMD)
    assert gnome.argv[1:] == ["--", *CMD.argv] and gnome.cwd == CMD.cwd
    xte = terminals.plan("x-terminal-emulator", TERMINALS.get("x-terminal-emulator").impl, CMD)
    assert xte.argv[1] == "-e" and "hi there" in xte.argv[2]


def test_choose_respects_platform_and_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(terminals.sys, "platform", "linux")
    monkeypatch.setattr(
        terminals.shutil, "which", lambda name: "/usr/bin/gnome-terminal" if name == "gnome-terminal" else None
    )
    assert terminals.choose()[0] == "gnome-terminal"
    assert terminals.describe() == "gnome-terminal (/usr/bin/gnome-terminal), chosen automatically"
    with pytest.raises(RegistryError, match="is for win32, not linux"):
        terminals.choose("wt")
    with pytest.raises(RegistryError, match="needs `x-terminal-emulator` on PATH"):
        terminals.choose("x-terminal-emulator")
    with pytest.raises(RegistryError, match="did you mean 'wt'"):
        terminals.choose("wtt")
    monkeypatch.setattr(terminals.shutil, "which", lambda name: None)
    with pytest.raises(RegistryError, match="--here"):
        terminals.choose()
    assert terminals.describe().startswith("No terminal emulator found")
    monkeypatch.setattr(terminals.sys, "platform", "win32")
    assert terminals.choose() == ("console", TERMINALS.get("console").impl)  # built in, nothing on PATH needed


def test_launch_spawns_the_planned_process(monkeypatch: pytest.MonkeyPatch):
    spawned = []
    monkeypatch.setattr(terminals.sys, "platform", "linux")
    monkeypatch.setattr(terminals.shutil, "which", lambda name: f"/bin/{name}" if name == "gnome-terminal" else None)
    monkeypatch.setattr(terminals.subprocess, "Popen", lambda argv, **kw: spawned.append((argv, kw)))
    assert terminals.launch(CMD) == "gnome-terminal"
    assert spawned[0][0][:2] == ["/bin/gnome-terminal", "--"] and spawned[0][1]["cwd"] == CMD.cwd
    assert isinstance(spawned[0][0], list) and Spawn(spawned[0][0]).argv == spawned[0][0]


# ---------------------------------------------------------------- profile sources


def _cfg(tmp_path: Path, **extra):
    settings = load_settings(tmp_path)
    cfg = settings.config_for(REPO)
    for key, value in extra.items():
        setattr(cfg, key, value)
    return cfg


def test_profile_sources_follow_the_precedence(tmp_path: Path):
    assert tuple(PROFILE_SOURCES.names()) == PRECEDENCE
    shared = tmp_path / "team-profiles"
    (shared / "octo").mkdir(parents=True)
    (shared / "octo" / "courier.toml").write_text(PROFILE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = _cfg(
        tmp_path,
        profile_source_options={"shared": PROFILE_SOURCES.options_for("shared", {"dirs": [str(shared)]}, where="w")},
    )
    assert [s.kind for s in profile_sources(cfg)] == ["shared"]
    cfg.local_profile_path.parent.mkdir(parents=True)
    cfg.local_profile_path.write_text(PROFILE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    assert [s.kind for s in profile_sources(cfg)] == ["local", "shared"]
    assert [s.kind for s in profile_sources(cfg, first_only=True)] == ["local"]
    explicit = tmp_path / "mine.toml"
    explicit.write_text(PROFILE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    cfg.profile = explicit
    assert [s.kind for s in profile_sources(cfg)] == ["explicit", "local", "shared"]
    assert profile_sources(cfg)[0].load().repo == REPO


def test_a_missing_explicit_profile_fails_loudly(tmp_path: Path):
    cfg = _cfg(tmp_path, profile=tmp_path / "gone.toml")
    with pytest.raises(ProfileError, match="doesn't exist"):
        profile_sources(cfg)


def test_repo_source_uses_the_forge_only_when_given(tmp_path: Path):
    class Forge:
        config_dir = ".github"

        def read_files(self, paths):
            assert paths == [".github/readthrough.toml", ".readthrough.toml", "readthrough.toml"]
            return {
                paths[
                    0
                ]: 'schema = 1\nrepo = "octo/courier"\n[[layers]]\nid = "a"\nlabel = "A"\n[[components]]\nid = "c"\nlabel = "C"\nlayer = "a"\npaths = ["**"]\n'
            }

    cfg = _cfg(tmp_path)
    assert profile_sources(cfg) == []
    sources = profile_sources(cfg, Forge())
    assert [s.kind for s in sources] == ["repo"] and sources[0].path is None
    assert sources[0].load().repo == "octo/courier"


def test_shared_profile_path_matches_case_insensitively(tmp_path: Path):
    (tmp_path / "Octo").mkdir()
    written = tmp_path / "Octo" / "Courier.toml"
    written.write_text("", encoding="utf-8")
    found = shared_profile_path(tmp_path, "github/octo/courier")
    # On a case-insensitive filesystem (macOS, Windows) the exact-name probe may answer first; either way it's that file.
    assert found is not None and found.samefile(written)
    assert shared_profile_path(tmp_path, "github/octo/other") is None
    assert shared_profile_path(tmp_path / "missing", "github/octo/courier") is None
    (tmp_path / "gitlab" / "grp").mkdir(parents=True)
    nested = tmp_path / "gitlab" / "grp" / "proj.toml"
    nested.write_text("", encoding="utf-8")
    assert shared_profile_path(tmp_path, "gitlab/grp/proj") == nested
    assert shared_profile_path(tmp_path, "gitlab/octo/courier") is None, "the GitHub layout is only for GitHub repos"


# ---------------------------------------------------------------- config keys


def test_config_validates_plugin_choices_and_options(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        'terminal = "wt"\nassistant = "rules"\n\n[terminals.wt]\nprofile = "PowerShell"\n\n'
        '[profile_sources.shared]\ndirs = ["./team", "~/more"]\n',
        encoding="utf-8",
    )
    settings = load_settings(tmp_path)
    assert (settings.terminal, settings.assistant) == ("wt", "rules")
    assert settings.terminal_options["wt"].profile == "PowerShell"
    assert settings.profile_source_options["shared"].dirs == [tmp_path / "team", Path.home() / "more"]
    cfg = settings.config_for("o/r")
    assert (cfg.terminal, cfg.assistant, cfg.terminal_options) == ("wt", "rules", settings.terminal_options)

    for text, message in [
        ('terminal = "wtt"\n', "did you mean 'wt'"),
        ('assistant = "copilto"\n', "did you mean 'copilot'"),
        ("terminal = 3\n", "terminal must be a name"),
        ('[terminals.wt]\nprofil = "x"\n', "did you mean 'profile'"),
        ("[assistants]\nrules = 1\n", "one \\[assistants.<name>\\] table"),
        ('[profile_sources.shared]\ndirs = "team"\n', "dirs must be a list"),
        ("[terminals.nope]\nx = 1\n", "unknown terminal 'nope'"),
    ]:
        config.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError, match=message):
            load_settings(tmp_path)
