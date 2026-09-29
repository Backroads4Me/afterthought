"""Redaction: the secret shapes that must not reach the archive, the index or a model.

Design section 12 makes redaction a rule of capture rather than a cleanup: built-in
secret patterns plus Ted's own, applied before anything is archived. The list of shapes
is the one BUILD-PLAN 5 (M2.3) names - private key blocks, the ``sk-``, ``ghp_``,
``github_pat_``, ``xox*`` and AWS access key id tokens, ``Authorization:`` headers,
password-style assignments, and credentials in URLs - and patterns from
``capture.redact_patterns`` are applied after it, adding to the list rather than
replacing it.

Every match becomes the same ``[redacted]`` marker, and the whole match goes, so the
output says nothing about what was removed: not its length, not its prefix, not its
kind. When part of what a pattern recognizes should survive, the pattern is written to
match only the part that must not - the URL pattern takes the credential and leaves the
host. The marker matches none of the patterns, so redacting redacted text changes
nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

REDACTED = "[redacted]"

_FLAGS = re.IGNORECASE

# A private key block, recognized on its BEGIN line. The body runs to the END line, or to
# the end of the text when there is none: the filter caps an entry on a word boundary
# (M2.3), so a block whose END line was cut away still holds key material in what remains.
_KEY_BLOCK = (
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    r"[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)"
)

# Token families, each recognized on its prefix rather than on the service named beside
# it. The character classes cover the prefixes BUILD-PLAN 5 names - ghp_ and xoxb-/xoxp- -
# and their sibling token types, since a token of a type not on the list is still a token.
_TOKENS = (
    # Model and vendor API keys: OpenAI, Anthropic, and everything else that is sk- prefixed.
    r"sk-[A-Za-z0-9_-]{8,}",
    # GitHub: ghp_ personal access tokens, and the ghs_/gho_/ghu_/ghr_ server and app tokens.
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    # Slack: bot and user tokens first, and the app, refresh and session tokens beside them.
    r"xox[baprs]-[A-Za-z0-9-]{8,}",
    # An AWS access key id: twenty characters, AKIA long-term or ASIA from temporary creds.
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
)

# An Authorization header, scheme and all: which scheme was used says nothing a digest
# needs, and the value runs to the end of the line.
_HEADER = r"\bauthorization\s*:\s*[^\r\n]*"

# A password-style assignment: an option, a key, or a field whose name says it holds a
# secret, with the value that follows it. The name may be one word of a longer identifier
# (AWS_SECRET_ACCESS_KEY) or a command-line option (--password), and the value may be
# quoted and hold spaces. A value beginning with the marker is already redacted, so
# redaction is never re-applied to its own output.
_ASSIGNMENT = (
    r"[\w-]*"
    r"(?:passwor\w*|passwd|pwd|secret\w*|token\w*|api\w*key\w*|access\w*key\w*"
    r"|private\w*key\w*|credential\w*)"
    r"[\w-]*"
    r"\s*(?:=|:)\s*(?:\"[^\"]*\"|'[^']*'|(?!\[)[^\s,;)\]}]+)"
)

# Credentials in a URL: the userinfo run up to and including the @, so the host the
# session was talking to stays. The fixed lookbehind is what tells a URL's userinfo from
# an email address in prose.
_URL_CREDENTIAL = r"(?<=://)[^\s/@:]+(?::[^\s/@]*)?@"

_BUILTIN_SOURCES: tuple[str, ...] = (
    _KEY_BLOCK,
    *_TOKENS,
    _HEADER,
    _ASSIGNMENT,
    _URL_CREDENTIAL,
)

_BUILTIN: tuple[re.Pattern[str], ...] = tuple(
    re.compile(source, _FLAGS) for source in _BUILTIN_SOURCES
)


class BadPattern(Exception):
    """A pattern from ``capture.redact_patterns`` that is not a regular expression."""


def redact(text: str, patterns: Iterable[str] = ()) -> str:
    """``text`` with every built-in secret shape and every pattern in ``patterns``
    replaced by ``[redacted]``.

    A pattern that does not compile raises ``BadPattern`` naming it: a configured pattern
    that cannot be applied would quietly stop redacting whatever it was written for, which
    is the failure this module exists to prevent. The pattern text is the problem, so it is
    named; no text ever matched against it is.
    """
    out = text
    for builtin in _BUILTIN:
        out = builtin.sub(REDACTED, out)
    for source in patterns:
        try:
            pattern = re.compile(source, _FLAGS)
        except re.error as exc:
            raise BadPattern(f"{source!r} is not a regular expression: {exc}") from exc
        out = pattern.sub(REDACTED, out)
    return out
