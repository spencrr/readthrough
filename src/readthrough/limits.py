"""Fixed safety limits. Deliberately not tunable: a config edit shouldn't be able to loosen a safeguard.

Everything here bounds input from outside readthrough (web requests, people's text, model output) or decides
what the browser may change. Operational settings that are safe to change live in ``tuning.py``.
"""

from __future__ import annotations

import re

# The largest request body the local server reads (a profile being edited in the browser is the biggest).
MAX_REQUEST_BYTES = 250_000
# An oversized body up to this size is read and discarded before the 413, so the client sees the answer
# instead of a connection reset (Windows resets sockets closed with unread data). Bigger ones are just cut off.
MAX_DISCARD_BYTES = 8_000_000
# The longest text field kept in a study-log entry (an answer, a note, a model's assessment).
MAX_STUDY_TEXT = 8000
# A login as the browser may send it: a GitHub login (enterprise managed users look like shortcode_suffix, so
# underscores are allowed), or the email address Azure DevOps and GitLab use as a unique name.
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.+-]{0,60})(?:@[A-Za-z0-9.-]{1,120})?(?:\[bot\])?$")
# A Copilot model name as accepted from the browser's settings page.
MODEL_RE = re.compile(r"^[\w.:-]{1,80}$")
# Bounds on per-repo settings the browser may change.
BROWSER_EDITABLE_INTS = {"session_minutes": (10, 480), "limit": (1, 500)}
# The most a code search tool returns to the study assistant, whatever the tool-output setting says.
MAX_SEARCH_OUTPUT = 20_000
