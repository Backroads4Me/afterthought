# Hook payload fixtures

One payload per agent lifecycle event, in the shape that agent documents. The parsers in
`src/aft/adapters/` are built against these files, not against prose.

| Files | Where the shape comes from |
|---|---|
| `claude-*.json` | Claude Code 2.1.283 hook input schemas for `SessionStart`, `PreCompact` and `SessionEnd`. Every payload carries `session_id`, `transcript_path` and `cwd`; each event adds what set it off: `source`, `trigger`, `reason`. |
| `pi-*.json` | pi 0.87.1 extension event names (`session_start`, `session_before_compact`, `session_shutdown`) and the `reason` each of those events carries, plus the session id, cwd and session file pi's session manager exposes. pi runs no shell hook of its own: its extension writes this payload (M1.4). |

Paths, session ids and timestamps are synthetic. The real captures collected at
BUILD-PLAN 6.1 step 1 replace these files, and a capture whose shape differs changes the
adapter beside it.
