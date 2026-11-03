"""The agent adapters: one module per agent, each turning that agent's hook payload
into a SessionEvent (DESIGN 5). Adding an agent means adding one module here and one
entry in `PARSERS`, plus one in `OUTPUT_LIMITS` when the agent cuts what a hook prints.
"""

from aft.adapters.claude import OUTPUT_LIMIT as CLAUDE_OUTPUT_LIMIT
from aft.adapters.claude import parse_claude
from aft.adapters.pi import parse_pi

PARSERS = {
    "claude": parse_claude,
    "pi": parse_pi,
}

# The most a hook may print and have all of it reach the session, by agent.
OUTPUT_LIMITS = {"claude": CLAUDE_OUTPUT_LIMIT}
