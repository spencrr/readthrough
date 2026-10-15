"""Add a place profiles can come from, through the registry, and watch the config loader validate its options.

Profile sources, terminal launchers, and study assistants are all plugins of the same shape: a name, a
one-line summary, an implementation, and a dataclass of options. This example registers a source that reads
profiles from a single JSON "catalog" file, then shows the loader rejecting a typo in its options.

    uv run python examples/custom_profile_source.py
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from readthrough import profile as profile_mod
from readthrough.config import Config
from readthrough.profile import PROFILE_SOURCES, ProfileSource, parse_profile
from readthrough.registry import RegistryError


@dataclass(frozen=True)
class CatalogOptions:
    path: Path = Path("profiles.json")  # relative paths are taken from the config file's directory


@PROFILE_SOURCES.add("catalog", "one JSON file mapping owner/name to a profile", options=CatalogOptions)
def _catalog(cfg: Config, github: Any, options: CatalogOptions) -> ProfileSource | None:
    if not options.path.is_file():
        return None
    catalog = json.loads(options.path.read_text(encoding="utf-8"))
    raw = catalog.get(cfg.repo.lower())
    if raw is None:
        return None
    label = f"{options.path.name}[{cfg.repo}]"
    return ProfileSource("catalog", label, lambda: parse_profile(raw, origin=label), options.path)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        catalog = home / "profiles.json"
        catalog.write_text(
            json.dumps(
                {
                    "octo/courier": {
                        "schema": 1,
                        "repo": "octo/courier",
                        "layers": [{"id": "core", "label": "Core"}],
                        "components": [{"id": "all", "label": "Everything", "layer": "core", "paths": ["**"]}],
                    }
                }
            ),
            encoding="utf-8",
        )
        cfg = Config(home=home, repo="octo/courier")

        # The new source only takes part once it's in the precedence list; plugins never pick their own priority.
        profile_mod.PRECEDENCE = (*profile_mod.PRECEDENCE, "catalog")
        cfg.profile_source_options = {
            "catalog": PROFILE_SOURCES.options_for(
                "catalog", {"path": "profiles.json"}, where="[profile_sources.catalog]", base=home
            )
        }
        sources = profile_mod.profile_sources(cfg)
        print("sources, highest precedence first:", [(s.kind, s.label) for s in sources])
        print("loaded:", sources[0].load().repo, "->", [c.label for c in sources[0].load().components])

        # Options are validated against the dataclass, with a suggestion for a typo.
        try:
            PROFILE_SOURCES.options_for("catalog", {"paht": "x"}, where="[profile_sources.catalog]")
        except RegistryError as exc:
            print("rejected:", exc)
        try:
            PROFILE_SOURCES.get("catalogue")
        except RegistryError as exc:
            print("rejected:", exc)


if __name__ == "__main__":
    main()
