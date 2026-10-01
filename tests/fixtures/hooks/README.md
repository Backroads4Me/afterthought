# Hook payload fixtures

At least one payload per agent lifecycle event, one per variant worth pinning, in the shape
the emitting side actually produces. The parsers in `src/aft/adapters/` are built against
these files, not against prose.

| Files | Where the shape comes from |
|---|---|
| `claude-*.json` | Real payloads captured from Claude Code 2.1.283 on 2026-09-28, copied in unchanged. They came from one throwaway `claude -p` session run with a temporary `--settings` file: `SessionStart` (startup) and `SessionEnd`, then a `--resume` of that session with `/compact`, which fired `SessionStart` (resume), `PreCompact` (manual), `SessionStart` (compact) and `SessionEnd`. So each carries that capture session's real `session_id`, `transcript_path` and `cwd`, and fields afterthought does not read: `hook_event_name`, `scratchpad_dir`, `prompt_id`, `custom_instructions`, `model`, and the four counters a resumed `SessionStart` adds. |
| `pi-*.json` | The payload our own extension writes for the pi extension event it handles (`session_start`, `session_before_compact`, `session_compact`, `session_shutdown`), matching pi 0.99.1: the session id, cwd and session file pi's session manager exposes, plus the `reason` that event carries, and a `hook_event_name` repeating the event the command line already names, which afterthought does not read. |

BUILD-PLAN M1.1's first step is the authority for these captures: it requires real
payloads over documentation and names the Claude Code 2.1.283 set. A capture whose shape
differs changes the adapter beside it.
