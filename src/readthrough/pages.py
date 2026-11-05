"""The served pages, assembled from ``assets/web``: one self-contained HTML file each, with the data inlined.

Each page is kept as separate ``.html``, ``.css``, and ``.js`` sources so each part can be edited as what it
is; ``page_source`` inlines them. Every script carries a per-render nonce and the page's content security
policy runs nothing else, so even third-party HTML that slipped past sanitizing could not run. The vendored
libraries are listed in ``VENDOR_SCRIPTS`` and live under ``assets/vendor`` with their licenses.
"""

from __future__ import annotations

import json
import secrets
from importlib.resources import files
from typing import Any

VENDOR_SCRIPTS = (
    "cytoscape.min.js",
    "layout-base.js",
    "cose-base.js",
    "cytoscape-fcose.js",
    "marked.umd.js",
    "purify.min.js",
    "lucide-icons.js",
)


def _vendor_js() -> str:
    vendor = files("readthrough").joinpath("assets/vendor")
    parts = [vendor.joinpath(name).read_text(encoding="utf-8") for name in VENDOR_SCRIPTS]
    return "\n;\n".join(parts).replace("</script", "<\\/script")


def page_source(name: str) -> str:
    """Assemble a page from its sources in assets/web: markup, shared theme plus page CSS, and page script.

    The result is still one self-contained file; the split only exists so each part can be edited as what it is.
    """
    web = files("readthrough").joinpath("assets/web")

    def part(file: str) -> str:
        return web.joinpath(file).read_text(encoding="utf-8").removesuffix("\n")

    css = "\n".join(p for p in (part("theme.css"), part(f"{name}.css")) if p)
    return (
        part(f"{name}.html")
        .replace("/*__READTHROUGH_CSS__*/", css)
        .replace("/*__READTHROUGH_APP__*/", part(f"{name}.js"))
    )


def render_setup(live: dict[str, Any], nonce: str) -> str:
    """The onboarding and settings page. Only ever served live: it's all API calls."""
    page = page_source("setup")
    icons = files("readthrough").joinpath("assets/vendor/lucide-icons.js").read_text(encoding="utf-8")
    return (
        page.replace("__NONCE__", nonce)
        .replace("/*__READTHROUGH_ICONS__*/", icons.replace("</script", "<\\/script"))
        .replace("/*__READTHROUGH_LIVE__*/null", inline_json(live))
    )


def inline_json(value: Any) -> str:
    r"""JSON that can sit inside a <script> element whatever strings it carries.

    ``<`` becomes ``\u003c``, which JSON reads as the same character, so neither ``</script>`` nor ``<!--`` (which
    puts the HTML tokenizer into a state where the real ``</script>`` no longer counts) can appear in the page.
    """
    return json.dumps(value).replace("<", "\\u003c")


def render_html(payload: dict[str, Any], *, live: dict[str, Any] | None = None, nonce: str | None = None) -> str:
    """Inline the libraries, the data, and (when served) the live-mode settings into the single-file dashboard.

    Every script carries a per-render nonce, and the page's CSP only runs scripts with that nonce. PR
    descriptions are third-party HTML, so even if something slipped past sanitizing it could not run.
    """
    nonce = nonce or secrets.token_urlsafe(16)
    return (
        page_source("dashboard")
        .replace("__NONCE__", nonce)
        .replace("/*__READTHROUGH_VENDOR__*/", _vendor_js())
        .replace("/*__READTHROUGH_LIVE__*/null", inline_json(live))
        .replace("/*__READTHROUGH_DATA__*/null", inline_json(payload))  # last: nothing in the data is a placeholder
    )
