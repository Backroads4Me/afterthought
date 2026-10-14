"""Fenced code in text that is read for structure: where a block opens and closes.

Pages, digests and prompts quote code, and a line inside a quoted block is the block's
text rather than the structure around it - a ``#`` line inside a quoted command is the
command's comment, not a heading, and a word inside a quoted sample is not part of what
the text asks. One walker answers for every reader which lines belong to a block.
"""

from __future__ import annotations

import re

# A block opens at a marker line: up to three spaces of indentation, then three or more of
# the same character, and whatever follows the opening marks names the language. Only a
# line of those marks alone - same character, at least as many of them, nothing after -
# closes the block, so the marks of quoted markup stay quoted, and text that ends inside a
# block ends there.
FENCE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")


class Fences:
    """Fence state across the lines of one text, fed line by line.

    ``fenced`` answers for each line whether it belongs to a fenced block: a block's
    opening line, every line between its markers and its closing line do, and the text
    after the closing line does not. A different run of marks inside a block does not
    close it, and a marker line never reads as the text around it.
    """

    def __init__(self) -> None:
        self._marks = ""

    def fenced(self, line: str) -> bool:
        """Whether ``line`` belongs to a fenced block, moving the state past it."""
        marker = FENCE.match(line)
        if marker is None:
            return bool(self._marks)
        marks, rest = marker.group(1), marker.group(2)
        if not self._marks:
            self._marks = marks
            return True
        if marks[0] == self._marks[0] and len(marks) >= len(self._marks) and not rest.strip():
            self._marks = ""
        return True
