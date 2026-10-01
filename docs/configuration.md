# Configuration

Every setting lives in `~/.afterthought/config.toml`, which `aft init` writes with the
defaults and a comment on each. Every setting is optional. An unknown key or a value of the
wrong type is an error, reported when any `aft` command starts.

The one thing the file cannot hold is its own location: set the `AFT_HOME` environment
variable to use a different home directory, for a scratch run or a test. Normal use never
sets it.

## Top level

| Setting | Default | What it does |
|---|---|---|
| `vault` | `"~/afterthought"` | The memory vault: a git repository and an Obsidian vault. Must be an absolute path (a leading `~` is fine). |
| `host` | the short hostname | The name this machine uses in project keys, `host:path`. Set it if your hostname changes. |

## `[model]`

One OpenAI-compatible endpoint serves every model call: digests and dream decisions.

| Setting | Default | What it does |
|---|---|---|
| `model.endpoint` | `"http://localhost:8000/v1"` | The endpoint's base URL. |
| `model.name` | `""` | The model name to request. |
| `model.api_key_env` | `""` | The name of an environment variable holding the endpoint's API key, sent as a bearer token. afterthought stores the variable's name, never the key. Empty means no key. For the background timer, put the variable in `~/.afterthought/env` (mode 600). |
| `model.extra_body` | `{}` | Fields added to every request, for what one endpoint needs. A reasoning model served by vLLM that spends its whole answer thinking is told not to with `extra_body = { chat_template_kwargs = { enable_thinking = false } }`. It may not set `model`, `messages`, `max_tokens` or `response_format`. |

## `[capture]`

| Setting | Default | What it does |
|---|---|---|
| `capture.worker_paths` | `[]` | Directories whose sessions are worker sessions: archived and searchable, digested only with spare budget, and never promoted to memory without a primary session's support. Orchestrator and validation worktrees belong here. |
| `capture.exclude_paths` | `[]` | Directories whose sessions are never captured. |
| `capture.exclude_projects` | `[]` | Projects, by id or slug, whose sessions are never captured, whatever directory they ran in. |
| `capture.redact_patterns` | `[]` | Extra regular expressions to redact, on top of the built-in secret patterns. Case-insensitive. A pattern that does not compile is an error. Applies to new evidence; use `aft forget pattern` for what is already archived. |

## `[evidence]`

| Setting | Default | What it does |
|---|---|---|
| `evidence.retention_days` | `90` | Days to keep filtered conversation in the evidence archive. Facts keep their quotes after their evidence expires. |

## `[dream]`

| Setting | Default | What it does |
|---|---|---|
| `dream.interval_minutes` | `30` | How often the timer runs background work. Rerun `aft install timer` after changing it. |
| `dream.quiet_minutes` | `15` | How long a project must be quiet, with no session events and no hand edits in the vault, before a dream run touches it. |
| `dream.short_term_sessions` | `5` | How many of a project's sessions short-term memory covers. A line rolls off once its newest source is older than this many sessions, and becomes long-term once its sources are this many sessions apart. At least 1. |

## `[work]`

| Setting | Default | What it does |
|---|---|---|
| `work.daily_token_budget` | `200000` | Tokens one day of background work may spend, counted from midnight UTC in `~/.afterthought/usage.jsonl`. When it is spent, the rest of the queue waits for the next day. `aft redigest` counts against it too. |

## `[inject]`

| Setting | Default | What it does |
|---|---|---|
| `inject.max_chars` | `8000` | The size cap of the memory block a session starts with, about 2,000 tokens. Lines are never cut in half: a line is in the block or left out. |

## `[library]`

| Setting | Default | What it does |
|---|---|---|
| `library.commit` | `true` | Commit imported library files to the vault's git repository. Switching it off keeps them on disk and out of git. |
