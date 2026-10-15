import pytest
from conftest import PROFILE_PATH

from readthrough.profile import FALLBACK, ProfileError, load_profile, parse_profile
from readthrough.rules import contracts_for

MINIMAL = {
    "schema": 1,
    "repo": "o/r",
    "layers": [{"id": "app", "label": "App"}, {"id": "support", "label": "Support", "source": False}],
    "components": [
        {"id": "api", "label": "API", "layer": "app", "paths": ["src/api/**"]},
        {
            "id": "core",
            "label": "Core",
            "layer": "app",
            "paths": ["src/**"],
            "responsibility": "Business logic",
            "does_not_own": "HTTP",
        },
        {"id": "tests", "label": "Tests", "layer": "support", "paths": ["tests/**"]},
    ],
}


def _with(**changes):
    return {**MINIMAL, **changes}


def test_minimal_profile_gets_a_fallback_and_defaults():
    p = parse_profile(MINIMAL)
    assert p.title == "r"
    assert p.component_of("src/api/x.py") == "api"
    assert p.component_of("src/x.py") == "core"
    assert p.component_of("README.md") == FALLBACK
    assert p.component_by_id[FALLBACK].layer == "support"
    assert p.read_order == ["api", "core", "tests", FALLBACK]
    assert p.tests_component == "tests"
    assert set(p.authors.kinds) >= {"internal", "external", "first-time", "bot", "unknown"}
    assert p.is_source("api") and not p.is_source("tests")


def test_inline_contracts():
    contracts = contracts_for(parse_profile(MINIMAL), None)
    assert contracts["core"].responsibility == "Business logic"
    assert contracts["core"].does_not_own == "- **Does not own**: HTTP"
    assert "api" not in contracts


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (_with(schema=2), "schema must be 1"),
        (_with(repo="nope"), "owner/name"),
        (_with(layers=[]), "layers"),
        (_with(colour="red"), "unknown top-level"),
        (_with(components=[{"id": "x", "label": "X", "layer": "nope", "paths": ["**"]}]), "unknown layer"),
        (_with(components=[{"id": "x", "label": "X", "layer": "app"}]), "path pattern"),
        (_with(components=[{"id": "x", "label": "X", "layer": "app", "paths": ["**"], "owner": "me"}]), "unknown key"),
        (_with(read_order=["ghost"]), "read_order"),
        (_with(smells=[{"id": "s", "kind": "convention", "message": "m", "line": "("}]), "bad regex"),
        (_with(signals=[{"id": "s", "kind": "scary", "message": "m", "paths": ["**"]}]), "kind must be"),
        (_with(authors={"association": {"MEMBER": "vip"}}), "unknown kind"),
        (_with(authors={"kinds": {"vip": {"group": "royalty"}}}), "group must be"),
    ],
)
def test_validation_errors_are_readable(raw, message):
    with pytest.raises(ProfileError, match=message):
        parse_profile(raw, origin="test.toml")


def test_custom_author_kind():
    p = parse_profile(
        _with(
            authors={
                "association": {"COLLABORATOR": "partner"},
                "kinds": {"partner": {"label": "partner team", "group": "internal", "priority": 5}},
            }
        )
    )
    assert p.authors.association["COLLABORATOR"] == "partner"
    assert p.authors.kinds["partner"].group == "internal" and p.authors.kinds["partner"].priority == 5


def test_toml_error_includes_the_file(tmp_path):
    bad = tmp_path / "p.toml"
    bad.write_text("schema = = 1", encoding="utf-8")
    with pytest.raises(ProfileError, match=r"p\.toml"):
        load_profile(bad)


def test_fixture_profile_uses_the_whole_schema():
    p = load_profile(PROFILE_PATH)
    assert p.component_of("courier/pipeline/steps/dedupe.py") == "steps"
    assert p.component_of("courier/pipeline/runner.py") == "pipeline"
    assert p.component_of("Makefile") == "infra"
    assert [layer.id for layer in p.layers if layer.flow] == ["orchestration"]
    assert p.contracts_source == "docs/architecture.md"
    assert {s.id for s in p.smells} >= {"async-suffix", "kw-only", "transport-store"}
