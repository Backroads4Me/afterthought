# Hook payload fixtures

At least one payload per agent lifecycle event, one per variant worth pinning, in the shape
the emitting side actually produces. The parsers in `src/aft/adapters/` are built against
these files, not against prose.

| Files | Where the shape comes from |
|---|---|
| `claude-*.json` | Real payloads captured from Claude Code 2.1.283 on 2026-09-28, copied in unchanged. They came from one throwaway `claude -p` session run with a temporary `--settings` file: `SessionStart` (startup) and `SessionEnd`, then a `--resume` of that session with `/compact`, which fired `SessionStart` (resume), `PreCompact` (manual), `SessionStart` (compact) and `SessionEnd`. So each carries that capture session's real `session_id`, `transcript_path` and `cwd`, and fields afterthought does not read: `hook_event_name`, `scratchpad_dir`, `prompt_id`, `custom_instructions`, `model`, and the four counters a resumed `SessionStart` adds. |
| `pi-*.json` | The payload our own M1.4 extension writes for the pi extension event it handles (`session_start`, `session_before_compact`, `session_shutdown`): the session id, cwd and session file pi's session manager exposes, plus the `reason` that event carries, and a `hook_event_name` repeating the event the command line already names, which afterthought does not read. pi runs no shell hook of its own, so there is no pi capture to copy until M1.4 runs; those paths and that session id are synthetic. |

BUILD-PLAN 6.1 step 1 is what replaced the hand-written Claude payloads with the captures
above; a capture whose shape differs changes the adapter beside it.
