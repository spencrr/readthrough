"""Parse a repo's own review rules: architecture contracts in markdown, and .github/instructions applyTo globs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Contract:
    responsibility: str = ""
    does_not_own: str = ""


@dataclass
class Instruction:
    name: str
    patterns: list[str]
    headings: list[str]
    url: str
    regexes: list[re.Pattern[str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.regexes = [glob_to_regex(p) for p in self.patterns]

    def applies_to(self, path: str) -> bool:
        return any(r.match(path) for r in self.regexes)


def parse_contracts(
    text: str, *, responsibility: str = "Responsibility", does_not_own: str = "Does not own"
) -> dict[str, Contract]:
    """Return the responsibility and "does not own" text for every ``##`` section of an architecture doc.

    A section's responsibility is any ``**... <responsibility>**: text`` line (so "**Executor
    Responsibility**:" counts). Its "does not own" contract is the bullet starting ``- **<does_not_own>**``
    plus everything nested under it.
    """
    resp_re = re.compile(rf"^\*\*[^*]*{re.escape(responsibility)}\*\*:\s*(.+)$", flags=re.MULTILINE)
    sections: dict[str, Contract] = {}
    for chunk in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        heading, _, body = chunk.partition("\n")
        title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", heading).strip()
        sections[title] = Contract(
            " ".join(m.strip() for m in resp_re.findall(body)), _does_not_own_block(body, does_not_own)
        )
    return sections


def _does_not_own_block(body: str, label: str) -> str:
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        if not line.lstrip().startswith(f"- **{label}**"):
            continue
        indent = len(line) - len(line.lstrip())
        block = [line.strip()]
        for nxt in lines[idx + 1 :]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            if nxt.strip():
                block.append(nxt.strip())
        return "\n".join(block)
    return ""


def contracts_for(profile: Any, doc_text: str | None) -> dict[str, Contract]:
    """Each component's contract, keyed by component id: from the architecture doc or written inline."""
    sections = parse_contracts(doc_text, **profile.contracts_labels) if doc_text and profile.contracts_source else {}
    out: dict[str, Contract] = {}
    for comp in profile.components:
        if comp.responsibility or comp.does_not_own:
            dno = comp.does_not_own or ""
            out[comp.id] = Contract(
                comp.responsibility or "", f"- **Does not own**: {dno}" if dno and not dno.startswith("- ") else dno
            )
        elif comp.contract and comp.contract in sections:
            out[comp.id] = sections[comp.contract]
    return out


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a VS Code style glob (``**``, ``*``, ``?``, ``{a,b}``) into an anchored regex."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        elif pattern[i] == "{" and "}" in pattern[i:]:
            end = pattern.index("}", i)
            out += "(?:" + "|".join(re.escape(o) for o in pattern[i + 1 : end].split(",")) + ")"
            i = end + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile("^" + out + "$")


def parse_instruction(name: str, text: str, url: str) -> Instruction:
    apply_to = ""
    front = re.match(r"^---\s*\n(.*?)\n---", text, flags=re.DOTALL)
    if front:
        match = re.search(r"^applyTo:\s*(.+)$", front.group(1), flags=re.MULTILINE)
        apply_to = match.group(1).strip().strip("'\"") if match else ""
    patterns = [p.strip() for p in re.split(r",(?![^{]*\})", apply_to) if p.strip()]
    headings = [h.strip() for h in re.findall(r"^## (.+)$", text, flags=re.MULTILINE)][:14]
    return Instruction(name=name, patterns=patterns, headings=headings, url=url)
