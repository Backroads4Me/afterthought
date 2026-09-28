# Transcript fixtures

Hand-trimmed Claude Code and pi session files. Every line is an entry an agent
wrote, cut short and checked for anything credential-shaped, so a reader is
tested against the envelope a real transcript carries rather than one invented
for a test.

| Fixture | Source session | What it carries |
| --- | --- | --- |
| `claude-session.jsonl` | `~/.claude/projects/-home-ted-src-afterthought/75ecdcb9-0baf-4177-85c5-d35ca9eb59b2.jsonl`, plus two entries from `-tmp-claude-1000--home-ted-src-e4281394-c0c2-4c11-a515-c79fb8fc2a9e-scratchpad/798ac640-2ee5-4459-8624-939bb427bb97.jsonl` and `-home-ted-src-openHASP/a10266d6-23f7-446e-8f2a-6381ee60090c.jsonl` | a prompt, an assistant turn, a tool result the CLI flagged with `is_error`, an `isMeta` prompt meant for the agent, the `queue-operation`, `attachment` and `custom-title` entries around them, and the two assistant notices the CLI wrote for itself, one marked `isApiErrorMessage` and one carrying the synthetic model |
| `claude-compaction.jsonl` | `~/.claude/projects/-home-ted-src-librecoach/87f6e8cf-289a-414b-9b3e-0bcf3b64a031.jsonl` | the `compact_boundary` entry and the `isCompactSummary` message compaction writes, between turns that came before and after |
| `claude-resumed.jsonl` | `~/.claude/projects/-home-ted-src-ops/0ee84fb7-98e3-4541-93e9-d8730d251418.jsonl` | the entries of the session it resumed, still carried under that session's id, then the new turns under the file's own |
| `pi-session.jsonl` | `~/.pi/agent/sessions/--home-ted-src-node-red-test--/2026-09-21T04-34-34-249Z_01a0c23e-8c89-73de-a794-8cd0266d5889.jsonl` | the session header, model and thinking-level changes, the system message holding the prompt, turns with thinking, text and tool calls, and a `toolResult` with `isError` |
| `pi-compaction.jsonl` | `~/.pi/agent/sessions/--home-ted-src-firstmate--/2026-09-27T10-08-45-625Z_01a0e256-aa38-73dc-934f-1d09d00e2f5c.jsonl` | a `compaction` entry and an extension's `custom_message`, between the messages before and after them |
| `pi-resumed.jsonl` | `~/.pi/agent/sessions/--home-ted-src-firstmate--/2026-09-27T04-26-57-622Z_01a0e11d-bcd5-77d0-b062-2743e1b3630c.jsonl` | two branches off one parent entry: the one ending at the last line is the conversation, the other holds the user turn as it stood before `/tree` re-submitted it edited |

Trimming keeps the entry types, ids, parent ids, timestamps and message shapes and
cuts everything the readers do not look at: token usage, request ids, rendered
attachments, hook metadata and the stored prompt sections. Long text is cut to a
sentence, and cut at an em dash so the fixtures stay plain ASCII. One tool result
in `pi-resumed.jsonl` keeps the first line of its output, since the rest listed
environment variables. The two notices ending `claude-session.jsonl` keep the timestamps of
the sessions that hit them, so they follow rather than join the window they were added to.
