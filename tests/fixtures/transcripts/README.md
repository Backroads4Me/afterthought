# Transcript fixtures

Synthetic Claude Code and pi session files. Each line is written by hand in the
exact envelope a real transcript carries - the same entry types, field names,
nesting and id shapes the two agents write - so a reader is tested against the
format it will actually meet, and no line comes from anyone's session.

| Fixture | Agent | What it carries |
| --- | --- | --- |
| `claude-session.jsonl` | Claude Code | a prompt, an assistant turn, a tool result the CLI flagged with `is_error`, an `isMeta` prompt meant for the agent, the `queue-operation`, `attachment` and `custom-title` entries around them, and the two assistant notices the CLI wrote for itself, one marked `isApiErrorMessage` and one carrying the synthetic model |
| `claude-compaction.jsonl` | Claude Code | the `compact_boundary` entry and the `isCompactSummary` message compaction writes, between turns that came before and after |
| `claude-resumed.jsonl` | Claude Code | the entries of the session it resumed, still carried under that session's id, then the new turns under the file's own, closing with a tool result that came back with a `tool_reference` and no text |
| `pi-session.jsonl` | pi | the session header, model and thinking-level changes, the system message holding the prompt, turns with thinking, text and tool calls, and a `toolResult` with `isError` |
| `pi-compaction.jsonl` | pi | a `compaction` entry and an extension's `custom_message`, between the messages before and after them |
| `pi-resumed.jsonl` | pi | two branches off one parent entry: the one ending at the last line is the conversation, the other holds the user turn as it stood before `/tree` re-submitted it edited |

Each fixture is one session: the record count, the entry types and the markers
the readers key on are what a test asserts through, so keep them when editing.
Long text is cut to a sentence and cut at a hyphen or a line break so the files
stay plain ASCII, and the tool output of a text-less result stays empty, since
that is what makes it a text-less result. The two notices ending
`claude-session.jsonl` keep timestamps a month before the window they sit in, so
they follow the conversation rather than join it.
