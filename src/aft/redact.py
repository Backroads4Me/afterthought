"""Redaction: the secret shapes that must not reach the archive, the index or a model.

Design section 12 makes redaction a rule of capture rather than a cleanup: built-in
secret patterns plus the user's own, applied before anything is archived. The list of shapes
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
    # The guard is that prefix: without it the family also matches the inside of any word
    # ending in those letters, and the archive loses a ticket id or a file name that was
    # never a secret. A hyphen is not a word character, so a key glued onto a name with one
    # - openai-sk-<key> - is still a prefix and still redacts.
    r"(?<!\w)sk-[A-Za-z0-9_-]{8,}",
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


def compile_patterns(sources: Iterable[str | re.Pattern[str]]) -> tuple[re.Pattern[str], ...]:
    """The configured ``sources`` as patterns, every one of them checked before any is used.

    A source that does not compile raises ``BadPattern`` naming it, and so does an empty or
    whitespace-only one: ``re.sub`` with an empty pattern matches between every character,
    so a stray empty entry in ``capture.redact_patterns`` would shred the very text it was
    written to protect. Checking happens here rather than at the first match so a caller can
    settle its configuration before it has any text to apply it to. A source already
    compiled is passed through, which is what lets that caller compile once and apply many.
    """
    compiled: list[re.Pattern[str]] = []
    for source in sources:
        if isinstance(source, re.Pattern):
            compiled.append(source)
            continue
        if not source.strip():
            raise BadPattern(f"{source!r} is not a pattern: it holds nothing to match")
        try:
            compiled.append(re.compile(source, _FLAGS))
        except re.error as exc:
            raise BadPattern(f"{source!r} is not a regular expression: {exc}") from exc
    return tuple(compiled)


def redact(text: str, patterns: Iterable[str | re.Pattern[str]] = ()) -> str:
    """``text`` with every built-in secret shape and every pattern in ``patterns``
    replaced by ``[redacted]``.

    ``patterns`` are the sources of ``capture.redact_patterns``, or those same patterns as
    ``compile_patterns`` returns them. A pattern that cannot be applied raises
    ``BadPattern`` naming it, since a configured pattern that quietly stops being applied
    leaves whatever it was written for in the clear.
    """
    out = text
    for pattern in (*_BUILTIN, *compile_patterns(patterns)):
        out = pattern.sub(REDACTED, out)
    return out
