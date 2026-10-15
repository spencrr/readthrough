from readthrough.rules import glob_to_regex, parse_contracts, parse_instruction

FRAMEWORK = """\
# Architecture

## [Converters](./converters/0_converters)

**Responsibility**: Converters convert prompts.

- **Does not own**: conversation state. Specifically not:
  - branching
  - scoring
- Another bullet that is not part of the block.

## Analytics

**Responsibility**: Make sense of results.
"""


def test_parse_contracts_extracts_contract_and_nested_does_not_own():
    sections = parse_contracts(FRAMEWORK)
    conv = sections["Converters"]
    assert conv.responsibility == "Converters convert prompts."
    assert conv.does_not_own.splitlines() == [
        "- **Does not own**: conversation state. Specifically not:",
        "- branching",
        "- scoring",
    ]
    assert sections["Analytics"].does_not_own == ""


def test_glob_double_star_and_braces():
    rx = glob_to_regex("frontend/**/*.test.{ts,tsx}")
    assert rx.match("frontend/src/a/b.test.tsx")
    assert rx.match("frontend/b.test.ts")
    assert not rx.match("frontend/src/b.ts")


def test_glob_leading_double_star_matches_root_and_nested():
    rx = glob_to_regex("**/tests/**")
    assert rx.match("tests/unit/x.py")
    assert rx.match("pkg/tests/x.py")
    assert not rx.match("courier/x.py")


def test_parse_instruction_splits_commas_outside_braces():
    text = '---\napplyTo: "courier/store/**/*.py, doc/**/*.{py,ipynb}"\n---\n\n# T\n\n## One\n## Two\n'
    ins = parse_instruction("db.md", text, "https://example/db.md")
    assert ins.patterns == ["courier/store/**/*.py", "doc/**/*.{py,ipynb}"]
    assert ins.headings == ["One", "Two"]
    assert ins.applies_to("doc/code/x.ipynb")
    assert not ins.applies_to("courier/models/x.py")


def test_parse_instruction_without_front_matter_has_no_patterns():
    assert parse_instruction("x.md", "# Just text", "u").patterns == []
