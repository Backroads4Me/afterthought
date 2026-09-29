"""The agent adapters: one module per agent, each turning that agent's hook payload
into a SessionEvent (DESIGN 5). Adding an agent means adding one module here and one
entry in `PARSERS`.
"""

from aft.adapters.claude import parse_claude
from aft.adapters.pi import parse_pi

PARSERS = {
    "claude": parse_claude,
    "pi": parse_pi,
}
