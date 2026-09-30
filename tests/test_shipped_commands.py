"""The commands shipped text tells an agent to run are commands the CLI has.

The library-import skill and the procedure ``aft library survey`` prints are instructions an
agent follows to the letter, so a command either of them names has to be one ``aft`` parses.
Each ``aft ...`` code span in both texts is checked against the real parser, the one
``aft.cli.main`` builds.

Three kinds of token are allowed after ``aft``: the name of a command or of one of its
actions, a ``<placeholder>`` standing for an argument, and an ``--option``. Any other bare
word is refused even where the parser would take it as the value of a positional argument,
because in shipped text such a word reads as an action name and the parser cannot tell the
two apart. A span that stops at a command or action name, such as ``aft library``, names it
rather than invoking it, and is accepted without its arguments.
"""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

import pytest

from aft import cli
from aft.library import survey

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "integrations" / "skills" / "library-import" / "SKILL.md"

_SPAN = re.compile(r"`(aft(?: [^`]*)?)`")
_PLACEHOLDER = re.compile(r"<[^<>\s]+>")


def commands(text: str) -> list[str]:
    """Every distinct ``aft ...`` code span in ``text``, in the order it first appears."""
    return list(dict.fromkeys(_SPAN.findall(text)))


def check(command: str) -> str | None:
    """Why ``command`` is not one the CLI takes, or None when it is."""
    tokens = shlex.split(command)[1:]
    parser = cli._parser()
    rest = list(tokens)
    while rest:
        choices = _actions(parser)
        if rest[0] not in choices:
            break
        parser = choices[rest.pop(0)]
    for token in rest:
        if not (_PLACEHOLDER.fullmatch(token) or token.startswith("-")):
            return f"{token!r} is not a command, an action, a <placeholder> or an option"
    if not rest:
        return None
    try:
        cli._parser().parse_args(tokens)
    except SystemExit:
        return "the parser refuses it"
    return None


def _actions(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """The commands or actions ``parser`` dispatches on; none for one that takes arguments."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


SHIPPED = {
    "the library-import skill": SKILL.read_text(encoding="utf-8"),
    "the survey procedure": survey.IMPORT_PROCEDURE,
}


@pytest.mark.parametrize("source", SHIPPED)
def test_every_command_shipped_text_names_parses(source, capsys) -> None:
    found = commands(SHIPPED[source])
    assert found, f"{source} names no aft command, so this test checks nothing"
    failures = [f"`{command}`: {why}" for command in found if (why := check(command))]
    capsys.readouterr()
    assert failures == [], f"{source} names a command aft does not have: " + "; ".join(failures)


@pytest.mark.parametrize(
    "command",
    [
        "aft key register <slug>",
        "aft key register",
        "aft library frobnicate <manifest>",
        "aft project add <path> <extra>",
    ],
)
def test_a_command_aft_does_not_have_is_refused(command, capsys) -> None:
    assert check(command) is not None
    capsys.readouterr()


@pytest.mark.parametrize(
    "command",
    ["aft library", "aft project add <path>", "aft install claude --dry-run", "aft lint"],
)
def test_a_command_aft_has_is_accepted(command, capsys) -> None:
    assert check(command) is None
    capsys.readouterr()
