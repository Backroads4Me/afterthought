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
| `model.api_key_env` | `""` | The name of an environment variable holding the endpoint's API key, sent as a bearer token. Afterthought stores the variable's name, never the key. Empty means no key. For the background timer, put the variable in `~/.afterthought/env` (mode 600). |
| `model.temperature` | `0` | How freely the model picks among likely words, from 0 to 2. At 0 it takes the likeliest word every time, so the same session gives the same memory. |
| `model.extra_body` | `{}` | Fields added to every request, for what one endpoint needs. A reasoning model served by vLLM that spends its whole answer thinking is told not to with `extra_body = { chat_template_kwargs = { enable_thinking = false } }`. It may not set `model`, `messages`, `max_tokens`, `temperature` or `response_format`. |

## `[capture]`

| Setting | Default | What it does |
|---|---|---|
| `capture.worker_paths` | `[]` | Directories whose sessions are worker sessions: archived and searchable, digested only with spare budget, and never promoted to memory without a primary session's support. Orchestrator and validation worktrees belong here. |
| `capture.exclude_paths` | `[]` | Directories whose sessions are never captured. |
| `capture.exclude_projects` | `[]` | Projects, by id or slug, whose sessions are never captured, whatever directory they ran in. |
| `capture.redact_patterns` | `[]` | Extra regular expressions to redact, on top of the built-in secret patterns. Case-insensitive. A pattern that does not compile is an error. Applies to new evidence; use `aft forget pattern` for what is already archived. |
| `capture.builtin_redaction` | `true` | Redact built-in secret shapes (private key blocks, provider-prefixed tokens, `Authorization:` headers, bearer tokens, URL credentials). `false` turns them off and applies only `capture.redact_patterns`. |

## `[evidence]`

| Setting | Default | What it does |
|---|---|---|
| `evidence.retention_days` | `90` | Days to keep filtered conversation in the evidence archive. Facts keep their quotes after their evidence expires. |


## `[digest]`

| Setting | Default | What it does |
|---|---|---|
| `digest.pipeline` | `turns` | How a digest asks its questions. `base` asks one model one broad question per chunk of filtered conversation. `turns` asks narrower ones over a compacted input instead: one call per batch of user turns for what the user settled, one for what the session observed, one for what a later item replaced, and one for the summary and handoff. |
| `digest.verify_items` | `false` | After a turns digest's final state, each kept `[stated]` item is asked back to the model once, over the entries around the item's own (the two user or assistant entries on either side of it, plus the entry an approval's proposal came from): did the user assert it rather than ask, propose or paste it, and does it still stand at the end of those entries? An item that fails either question is dropped, and the reason is recorded on the page. |
| `digest.verifier_endpoint` | `` (the `[model]` endpoint) | The endpoint the item check calls when `digest.verify_items` is on. Empty means the `[model]` endpoint, its key and `extra_body` included. When set, the check sends that endpoint the key `digest.verifier_api_key_env` names and no `extra_body` - the `[model]` table's key and request fields belong to its own endpoint. |
| `digest.verifier_model` | `` (the `[model]` name) | The model the item check calls when `digest.verify_items` is on. Empty means the `[model]` name. |
| `digest.verifier_api_key_env` | `` (no key) | The environment variable holding the verifier endpoint's API key, sent as a bearer token; empty means the verifier endpoint takes no key. Read only with `digest.verifier_endpoint` set. |
| `digest.keep_check` | `false` | When true, every fact a turns digest keeps is asked once in batches of up to 20 whether it passes the keep-test on its own: would a future session act differently, or ask the user again, without it; or is it a one-time order to do work, the status of work in the session, a step of troubleshooting that ended with the session, or a value the repository already records. A fact answered false is dropped, and the reason is recorded on the page. |

## `[dream]`

| Setting | Default | What it does |
|---|---|---|
| `dream.interval_minutes` | `30` | How often the timer runs background work. Rerun `aft install timer` after changing it. |
| `dream.quiet_minutes` | `15` | How long a project must be quiet, with no session events and no hand edits in the vault, before a dream run touches it. |
| `dream.short_term_sessions` | `5` | How many of a project's sessions short-term memory covers. A line rolls off once its newest source is older than this many sessions, and becomes long-term once its sources are this many sessions apart. At least 1. |
| `dream.short_term_days` | `7` | Short-term memory also expires by age: a line whose newest source is older than this many days rolls off at the project's next dream run, so a project that stops having sessions does not keep what it was last working with for good. At least 1. |

## `[work]`

| Setting | Default | What it does |
|---|---|---|
| `work.daily_token_budget` | `200000` | Tokens one day of background work, digests and dreaming alike, may spend, counted from midnight UTC in `~/.afterthought/usage.jsonl`. When it is spent, the rest of the queue waits for the next day, or for `aft budget reset`. `aft redigest` counts against it too. |
| `work.phrases` | `true` | Whether `aft run` runs the search-phrase step (BUILD-PLAN F85): the model writes short search phrases for the indexed lines and digest sections into `phrases/` vault files, under the same budget and cooldown as the digests. Turn it off to run digests without that extra spend, the way the eval harness does. |

## `[search]`

| Setting | Default | What it does |
|---|---|---|
| `search.rerank` | `true` | `aft search` asks the configured model to reorder its top candidates by how well each answers the question, and the model's order comes first; the candidates it leaves out follow in the index's own order, cut to `--limit`. When the model cannot answer - the setting off, the endpoint cooling down or unreachable, the answer late or unusable, or the day's search token budget spent - the search keeps the index's own order and says nothing about it on stdout. The reorder call is recorded in `usage.jsonl` like every model call, but never sets or clears the endpoint's cooldown, so a slow or failing search never pauses background work. |
| `search.rerank_timeout_seconds` | `5` | How long the reorder call may take before the search keeps the index's own order. |
| `search.daily_token_budget` | `200000` | Tokens one day of search reranks may spend, counted from midnight UTC in `~/.afterthought/usage.jsonl` apart from `work.daily_token_budget`: when it is spent, `aft search` keeps the index's own order for the rest of the day. |

## `[inject]`

| Setting | Default | What it does |
|---|---|---|
| `inject.max_chars` | `8000` | The size cap of the memory block a session starts with, about 2,000 tokens. Lines are never cut in half: a line is in the block or left out. |

## `[recall]`

What each submitted prompt brings back from memory (BUILD-PLAN F87). Every prompt you
send is searched, and up to `recall.max_hits` lines come back beside it, one line each
with its path, its heading and the matched text, under the heading "Memory that may bear
on this prompt (from aft search; check before relying on it):". Recall reads memory and
writes nothing - not the prompt, not a log of it - so a session whose capture is off
still recalls. Only curated memory comes back (page lines, digest sections, the library),
never an archive entry, and never a line the session's start block or its own digest
already holds. A prompt left with fewer than three content words recalls nothing, and a
worker session (`capture.worker_paths`) recalls nothing.

| Setting | Default | What it does |
|---|---|---|
| `recall.enabled` | `true` | Whether prompts bring memory lines back at all. `false` and the prompt hook prints nothing. |
| `recall.max_hits` | `3` | The most lines one prompt recalls. At least 1. |
| `recall.max_chars` | `1500` | The size cap of the whole recall block, heading included. Whole lines are dropped to fit, never cut in half. At least 1. |
| `recall.rerank` | `false` | Recall runs on every prompt, so it searches with BM25 alone by default. `true` borrows `aft search`'s model reordering, which can add seconds to a prompt. |

## `[library]`

| Setting | Default | What it does |
|---|---|---|
| `library.commit` | `true` | Commit imported library files to the vault's git repository. Switching it off keeps them on disk and out of git. |
