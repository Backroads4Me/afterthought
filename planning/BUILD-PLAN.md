# Afterthought build plan

This plan turns [DESIGN.md](DESIGN.md) into tasks that one worker can each complete alone.
It is written for firstmate to dispatch and for workers to build from. The design is the
authority on behavior; this plan is the authority on sequence, interfaces and done
conditions. Where they disagree, the design wins and the plan gets corrected.

M0 is built. Its code is the foundation every task below calls into, so read `src/aft/`
before starting any task.

## 1. Rules for the build

These rules apply to every task and every pull request.

1. **Every PR ships behavior or tests.** No PR edits only planning documents.
2. **`planning/` is read-only for workers.** It changes only when Ted decides to change the
   design. A worker that finds the design wrong or ambiguous stops and asks (section 4)
   instead of choosing on its own.
3. **One task, one PR**, titled with its task id (`M2.3: filter with off-the-record and
   redaction`). A task too large for one PR is split before starting, and the split is
   reported.
4. **Tests define done.** Each task lists its tests. `uv run pytest`, `uv run ruff check`
   and `uv run ruff format --check` pass on every PR.
5. **Tests never touch real state.** Every test runs with `AFT_HOME` and the vault in a
   temporary directory, a temporary git identity (see `tests/conftest.py`), and no network.
   Model calls go to the fake endpoint from M2.5.
6. **No new runtime dependencies** without asking. Today there is one: PyYAML. The model
   client uses the standard library.
7. **Reviews look at code and behavior** against the design section the task cites, never
   at the wording of planning documents.
8. **Size budget:** about 5,000 non-blank lines of Python in `src/` through M4. Count with
   `cat src/aft/*.py src/aft/**/*.py | grep -cvE '^\s*($|#)'`. M0 is about 1,160. Going
   past the budget is reported, not worked around.
9. **Never write to the real vault or `~/.afterthought`** during a task. Installing into
   the real environment (`aft install`, systemd units, hooks) is done by Ted, at milestone
   ends, from the checklists in section 6.

## 2. What M0 provides

| Module | Provides |
|---|---|
| `aft.config` | `load() -> Config` with `home`, `vault`, `host`, `settings` (every section of `config.toml`), `registry_path`, `lock_path`. Unknown keys and wrong types raise `ConfigError`. |
| `aft.gitutil` | `git(args, cwd, check)`, `repo_info(path) -> RepoInfo(toplevel, main, origin)`, `normalize_remote(url)`. |
| `aft.registry` | `Registry.load/save`, `add`, `add_path`, `add_remote`, `rename`, `remove`, `by_slug`, `by_id`; `Project(id, slug, title, paths, remotes, created)`; `path_key(host, path)`. |
| `aft.keys` | `resolve(cwd, registry, host, on_alias) -> Resolution(kind, cwd, project, slug, root, alias_added)`, kinds `PATH`, `REMOTE`, `UNREGISTERED`, `NONE`. |
| `aft.vault` | `init(config)`, `commit(vault, paths, message)`, `locked(config)` (the exclusive vault lock every writer takes), `require(config)`. |
| `aft.page` | `split_frontmatter(text)`, `parse(text, path) -> Page` with `facts: list[Fact]`; fact states `CANDIDATE`, `ACTIVE`, `SUPERSEDED`, `WITHDRAWN`, `STALE`, `CONFLICT`, `NEEDS_YOU`; `KIND_SECTIONS`. |
| `aft.lint` | `lint_vault(vault) -> list[Finding]`. |
| `aft.cli` | `aft init`, `aft key`, `aft project list/add/rename/forget`, `aft lint`. New commands are added here as subcommands. |

New code follows the same shape: small modules, dataclasses for records, pure functions
where possible, errors as exceptions caught once in `cli.main`.

## 3. Review checkpoints with `claude -p`

Firstmate's own gate reviews every PR. On top of that, the PRs and milestones below get an
independent review from Claude, run by the worker with `claude -p`:

| When | Why |
|---|---|
| The last PR of M1, M2, M3, M4, M7 | Milestone review against the design |
| Every PR touching `src/aft/dream/rules.py` or `src/aft/dream/apply.py` | The rules decide what becomes trusted memory |
| Every PR changing a model prompt or JSON schema | Prompts are where silent quality loss hides |

The worker runs, from the repository root:

```bash
git diff origin/main...HEAD > /tmp/aft-review.diff
claude -p --model claude-opus-5-5 --allowedTools "Read,Grep,Glob" \
  "$(cat planning/review-prompt.md)
Task: <task id and title>
Design sections: <sections the task cites>
The diff is in /tmp/aft-review.diff." > /tmp/aft-review.md
gh pr comment <pr> --body-file /tmp/aft-review.md
```

The worker then fixes every finding marked `blocking` in the same PR, or replies on the PR
explaining why a finding is wrong. Non-blocking findings are listed in the PR description
for Ted.

Two prerequisites, both Ted's:

- Firstmate's `captain.md` forbids running a Claude model unless Ted names it for the task.
  These checkpoints need a standing permission there, such as: "For afterthought, workers
  run `claude -p` reviews at the checkpoints listed in `planning/BUILD-PLAN.md` section 3."
- Each review spends Claude allowance. At roughly ten checkpoints plus the rules and prompt
  PRs, expect twenty to thirty reviews over the build.

## 4. When a worker is unsure

A worker that finds the design silent, contradictory, or wrong for the case in front of it
does not guess. It stops the task, writes the question and the options it sees on the PR
or to firstmate, and waits for Ted's answer. Firstmate relays the question to Ted.

## 5. Tasks

Each task lists: what it depends on, the design sections it implements, the files it
owns, the interface it provides, the behavior, the tests, and what is out of scope. Tasks
without a dependency on each other can run in parallel.

### M1 Recall

Done when: a cold session in each agent receives the same block, under the cap, in under
150 ms (DESIGN 15, M1).

#### M1.1 Normalized events and the hook command

- **Depends on:** M0. **Design:** 5 (adapter boundary), 8 (adapters).
- **Files:** `src/aft/events.py`, `src/aft/adapters/claude.py`, `src/aft/adapters/pi.py`,
  `src/aft/cli.py` (`aft hook`), `tests/test_adapters.py`, `tests/fixtures/hooks/`.
- **Interface:**

```python
@dataclass(frozen=True)
class SessionEvent:
    agent: str            # "claude" | "pi"
    kind: str             # "session_start" | "compaction" | "session_end"
    session_id: str
    cwd: Path
    transcript_path: Path | None
    time: datetime        # when the hook ran, UTC
    detail: str | None    # e.g. Claude's SessionStart source: startup, resume, clear, compact

def parse_claude(hook_event: str, payload: dict, now: datetime) -> SessionEvent
def parse_pi(hook_event: str, payload: dict, now: datetime) -> SessionEvent
```

- **Behavior:** `aft hook <agent> <event>` reads one JSON payload from stdin, builds a
  `SessionEvent`, and dispatches it: `session_start` prints the injection block (M1.2);
  `compaction` and `session_end` enqueue capture (M2.2; until then they do nothing).
  Claude Code events map `SessionStart`, `PreCompact` and `SessionEnd`; pi events map
  `session_start`, `session_before_compact` and `session_shutdown`. The command **always
  exits 0**, prints nothing on failure, logs failures to `~/.afterthought/logs/hooks.log`,
  and gives up after 2 seconds.
- **First step:** record one real payload per event from the installed Claude Code and pi
  versions into `tests/fixtures/hooks/` (Ted captures the Claude Code ones; see 6.1). Build
  the parsers against those files, not against documentation.
- **Tests:** each fixture parses to the expected event; malformed JSON, a missing field, or
  an unknown event exits 0 with no output and one log line.
- **Out of scope:** installing hooks (M1.3, M1.4).

#### M1.2 The injection block

- **Depends on:** M0. **Design:** 10 (injection at session start).
- **Files:** `src/aft/inject.py`, `src/aft/cli.py` (`aft inject [path]`),
  `tests/test_inject.py`.
- **Interface:** `build(config: Config, resolution: Resolution) -> str`.
- **Behavior:** reads files only; no model, no index. Layers in order, each trimmed to fit
  what remains of `inject.max_chars`, never cutting a fact line in half:
  1. `# Memory: <title>` and the overview's `description`
  2. last session: `handoff` from the newest digest in `digests/<slug>/` (empty until M2)
  3. active and conflict/needs-you facts from `projects/<slug>/overview.md` in the order
     State, Decisions, Lessons, Procedures, Facts, References; a conflict or question is
     marked `(disputed)` or `(waiting on Ted)` after the line; source tags and footnotes are
     stripped
  4. active facts from `global/preferences.md`
  5. one line per other page in `projects/<slug>/` and per reference collection whose
     `aft.projects` includes the project: `- <title>: <description> (aft read <path>)`
  6. `Search memory: aft search "<words>". Read a page: aft read <path>.`

  An unregistered or unresolved directory gets only layers 4 and 6. Candidates, stale,
  superseded and withdrawn lines are never included.
- **Tests:** layer order; the cap holds and never splits a line; excluded states stay out;
  an unregistered directory; 50 pages of 200 lines each build in under 150 ms on this host.
- **Out of scope:** per-prompt recall.

#### M1.3 Claude Code installation

- **Depends on:** M1.1. **Design:** 8 (adapters).
- **Files:** `src/aft/install/claude.py`, `src/aft/cli.py` (`aft install claude
  [--dry-run] [--uninstall]`), `tests/test_install_claude.py`.
- **Behavior:** merges three hook entries into `~/.claude/settings.json` (path taken from
  an argument so tests use a temporary file): `SessionStart`, `PreCompact`, `SessionEnd`,
  each running `aft hook claude <event>` with a 5-second timeout. It writes a backup
  first, never removes or reorders hooks it did not add, is idempotent, and `--uninstall`
  removes exactly its own entries. `--dry-run` prints the resulting JSON diff.
- **Tests:** install into an empty file, a file with other hooks, twice; uninstall leaves
  other hooks intact; invalid existing JSON aborts without writing.

#### M1.4 The pi extension

- **Depends on:** M1.1. **Design:** 8 (adapters).
- **Files:** `integrations/pi/afterthought.ts`, `src/aft/install/pi.py`, `src/aft/cli.py`
  (`aft install pi`), `tests/test_install_pi.py`.
- **Behavior:** the extension is thin, like the rtk extension in `~/.pi/agent/extensions/`:
  - on `session_start`: run `aft hook pi session_start` with the session id, cwd and
    session file (`ctx.sessionManager.getSessionFile()`) as JSON on stdin, and keep the
    printed block
  - on `before_agent_start`: add the kept block as its own system prompt section (not a
    replacement of the prompt)
  - on `session_compact`: re-run the hook as `compaction`, and refresh the kept block
  - on `session_shutdown`: run `aft hook pi session_end`, without waiting more than 2 s

  `aft install pi` copies the file to `~/.pi/agent/extensions/afterthought/index.ts`
  (target directory passed as an argument in tests).
- **Tests:** install and uninstall into a temporary directory. The extension itself is
  checked by the live check in 6.1.

#### M1.5 Seed pages for afterthought

- **Depends on:** M0. **Design:** 6.
- **Files:** a script `scripts/seed_afterthought.py` that writes
  `projects/afterthought/overview.md` into a vault given on the command line.
- **Behavior:** 10 to 20 facts about afterthought taken from `planning/DESIGN.md`, each
  `[observed]`, with a source whose `resource` is `file:planning/DESIGN.md#<heading>` and
  whose `quote` is copied from that section. The page passes `aft lint`.
- **Tests:** the script's output lints clean, and every quote appears in `DESIGN.md`.

### M2 Capture

Done when: scripted sessions in both agents produce digests whose quotes all verify; a
rerun adds nothing; an off-the-record span and planted secrets never reach the archive,
the index or the model; capture returns in under 50 ms; evidence past retention is purged.

#### M2.1 Transcript readers

- **Depends on:** M0. **Design:** 5 (adapter boundary).
- **Files:** `src/aft/readers/__init__.py`, `readers/claude.py`, `readers/pi.py`,
  `tests/test_readers.py`, `tests/fixtures/transcripts/`.
- **Interface:**

```python
@dataclass(frozen=True)
class Entry:
    id: str               # stable within the session: the agent's own entry id
    time: datetime
    role: str             # "user" | "assistant" | "tool"
    text: str             # user prompt, assistant final text, or tool output
    is_error: bool        # tool output flagged as an error by the agent

def read(agent: str, path: Path, after: str | None = None) -> Iterator[Entry]
```

- **Behavior:** yields entries after the `after` id in file order. Claude Code: skip
  `isMeta` entries, thinking blocks, and the summary entries it writes on compaction;
  split tool results out of user messages. pi: read `message` entries, follow the active
  branch only. Unknown entry types are skipped, never fatal.
- **Tests:** fixture transcripts (small, hand-trimmed from real sessions, secrets removed)
  for each agent, including a compaction, a tool error and a resumed session.

#### M2.2 Capture queue, capture hook and sweep

- **Depends on:** M1.1, M2.1. **Design:** 8 (capture queue).
- **Files:** `src/aft/queue.py`, `src/aft/capture.py`, `tests/test_queue.py`.
- **Interface:** `enqueue(config, event) -> None`; `next_job(config) -> Job | None`;
  `complete(config, job, watermark)`; `fail(config, job, error)`; `sweep(config, now)`.
- **Behavior:** the queue is `~/.afterthought/queue.sqlite` in WAL mode. `enqueue` writes one
  row inside `BEGIN IMMEDIATE` and returns; it never calls a model and never reads the
  transcript. A session has one watermark (the last digested entry id), updated only when
  its digest is written. `sweep` scans both agents' session directories for sessions
  modified since the last sweep that have no pending job and are idle for 30 minutes or
  have grown by 200 KB since their watermark, and enqueues them. Resolution to a project
  happens at digest time, not in the hook.
- **Tests:** two processes enqueue concurrently without loss; a crash between reading and
  completing leaves the job retryable; the hook path completes in under 50 ms with a
  1,000-row queue; the sweep finds a session whose hook never fired.

#### M2.3 Filter, off the record, and redaction

- **Depends on:** M2.1. **Design:** 8 (filter), 12.
- **Files:** `src/aft/filter.py`, `src/aft/redact.py`, `tests/test_filter.py`.
- **Interface:** `filter_entries(entries, config) -> list[Entry]`;
  `redact(text, patterns) -> str`.
- **Behavior:** in this order: drop off-the-record spans (a user entry whose text begins,
  after whitespace, with `aft off` or `off the record`, case-insensitive, trailing
  punctuation ignored, starts a span; `aft on` or `on the record` ends it; the marker
  entries themselves are dropped); keep user and assistant text; from tool entries keep
  only lines matching error patterns (`error`, `failed`, `Traceback`, `exception`,
  `FAILED`, non-zero exit) and `path:line` references; cap each entry at 1,500 characters
  on a word boundary with ` [...]`; redact. Built-in redaction covers private key blocks,
  `sk-`/`ghp_`/`github_pat_`/`xox[bp]-`/AWS key ids, `Authorization:` headers,
  `password=`-style assignments, and credentials in URLs, plus `capture.redact_patterns`.
  Redacted text becomes `[redacted]`.
- **Tests:** each marker form; a marker mid-sentence does nothing; an unclosed span runs to
  the end; each secret form; the cap; error-line extraction.

#### M2.4 Evidence archive and retention

- **Depends on:** M2.3. **Design:** 8 (evidence archive).
- **Files:** `src/aft/evidence.py`, `tests/test_evidence.py`.
- **Interface:** `store(config, agent, session_id, project_id, entries)`;
  `entries(config, session_id) -> list[Entry]`; `find_quote(config, session_id, entry_id,
  quote) -> bool`; `purge(config, now) -> int`; `forget(config, session_id)`.
- **Behavior:** `~/.afterthought/evidence.sqlite`, one row per filtered entry, text
  compressed with zlib. `purge` removes entries older than `evidence.retention_days`.
  `find_quote` compares whitespace-normalized text.
- **Tests:** round trip; idempotent store; purge boundary; quote matching ignores
  whitespace differences and nothing else.

#### M2.5 Model client and fake endpoint

- **Depends on:** M0. **Design:** 13.
- **Files:** `src/aft/model.py`, `tests/fakes/model_server.py`, `tests/test_model.py`.
- **Interface:** `complete_json(config, system: str, user: str, schema: dict, max_tokens:
  int) -> dict`; raises `ModelError`. Tracks tokens used per call for budgets.
- **Behavior:** POSTs to `{model.endpoint}/chat/completions` with `response_format:
  {"type": "json_schema", ...}` using `urllib`. Validates the response against the schema
  with a small validator in `aft/schema.py` (types, required keys, enums, string lengths;
  no dependency). Retries once on invalid output or a 5xx, then raises.
- **Tests:** against the fake server: valid response; invalid then valid; two invalids
  raise; timeout raises.

#### M2.6 Digest

- **Depends on:** M2.2, M2.3, M2.4, M2.5, M1.2. **Design:** 8 (digest).
- **Files:** `src/aft/digest.py`, `src/aft/prompts/digest.md`, `src/aft/prompts/digest.schema.json`,
  `src/aft/work.py` (`aft work`: sweep, then process jobs within budget),
  `tests/test_digest.py`.
- **Behavior:** for one job: resolve the project from the event's cwd; read entries after
  the watermark; filter; store evidence; render entries as `[<id>] <ROLE> <time>:\n<text>`;
  call the model with the prompt and schema below; verify each fact (quote found in its
  entry; `stated` only from a user entry; `kind` and `scope` valid); write the digest to
  `digests/<slug>/<yyyy>/<mm>/<date>-<agent>-<session>.md` as an OKF page (`type: Session
  Digest`) with the summary, handoff, and verified facts, each fact carrying its source
  entry and quote; record dropped facts and the reason in the digest's frontmatter under
  `aft.dropped`; advance the watermark; commit under the vault lock. A chunk over
  30,000 input tokens is split at entry boundaries. `aft work` stops when the daily token
  budget is spent and leaves the rest queued. The digest updates the injection's "last
  session" layer, so M1.2 reads `handoff` from it.
- **Prompt** (`src/aft/prompts/digest.md`):

```text
You read one part of a working session between Ted and an AI coding agent, and you
extract what a future session would need to know. You are building long-term memory, so
precision matters more than coverage. Return nothing rather than something uncertain.

Keep only what a future session could not learn by reading the code or the git history:
decisions and their reasons, preferences, how things are actually configured, lessons
learned the hard way, procedures, and where authoritative information lives. Keep what
was surprising or decided. Skip routine steps, file listings, code, and anything already
recorded in git.

For each fact:
- statement: one sentence, in plain present tense, that stands on its own.
- kind: preference, decision, state, fact, lesson, procedure, or reference.
- source:
  - stated: Ted said it and committed to it.
  - observed: seen in a file, a command's output, a log, or an error.
  - inferred: your own conclusion from the session.
  - suggested: an idea, a question, or a tentative remark nobody committed to.
  A tentative remark from Ted ("I think I'll switch to X", "maybe we should") is suggested,
  not stated.
- quote: copy the words that support the fact exactly as they appear in one entry, at
  most 300 characters. Do not paraphrase or join words from different entries.
- entry: the id of the entry the quote comes from.
- scope: global only for Ted's preferences and environment that apply to every project;
  otherwise project.

Also write:
- summary: two to four sentences on what happened.
- handoff: what was in flight when this part ended, approaches that failed, and what is
  still open. Use empty values when there is nothing to hand off.

Never invent a fact, a quote, or an entry id.
```

- **Schema** (`src/aft/prompts/digest.schema.json`): an object with `summary` (string,
  max 800), `handoff` (object: `in_flight` string max 400, `failed` array of strings max 5,
  `open` array of strings max 5), and `facts` (array, max 20) of objects with `statement`
  (max 300), `kind` (enum of the seven kinds), `source` (enum of the four tags), `quote`
  (max 300), `entry` (string), `scope` (enum `project`, `global`). All keys required.
- **Tests:** with the fake endpoint: a fabricated quote is dropped; `stated` on an
  assistant entry is dropped; the digest page lints; a rerun on the same session produces
  no new digest; the budget stops work and leaves jobs queued.
- **Review checkpoint:** yes (prompt and schema).

#### M2.7 Worker sessions and exclusions

- **Depends on:** M2.6. **Design:** 8 (worker sessions), 12 (exclusions).
- **Behavior:** a session whose cwd is under `capture.exclude_paths` is never read.
  One under `capture.worker_paths` is stored as evidence and indexed, but digested only
  when the day's budget has room after primary sessions; its digest is marked
  `aft.role: worker`, and its facts carry that role into the dream.
- **Tests:** excluded session leaves no trace; worker session is archived but not digested
  when the budget is short.

#### M2.8 Forget and redigest

- **Depends on:** M2.6. **Design:** 12, 8 (evidence archive).
- **Behavior:** `aft forget session <id>` removes evidence, index rows (once M3 exists),
  and digest files, and marks curated lines resting only on that session as withdrawn
  (once M4 exists; until then it lists them). `aft forget pattern <regex>` redacts
  matches in the archive and adds the pattern to `capture.redact_patterns` by printing the
  line for Ted to add; it never edits `config.toml` itself. Both print a note that the
  vault's git history still holds older commits. `aft redigest <session>` rebuilds a
  digest from archived evidence.
- **Tests:** forget removes every trace afterthought controls; redigest reproduces a
  digest from evidence alone.

#### M2.9 Timer units

- **Depends on:** M2.6. **Design:** 9 (dreaming runs from a timer).
- **Files:** `src/aft/install/systemd.py`, `aft install timer [--dry-run]`.
- **Behavior:** writes `aft-work.service` and `aft-work.timer` (every
  `dream.interval_minutes`) under `~/.config/systemd/user/`; the capture hook may start
  the service early with `systemctl --user start --no-block aft-work.service`, at most once
  every 5 minutes. Unit directory passed as an argument in tests.
- **Tests:** unit file contents; idempotent install.

### M3 Search

Done when: known-answer queries return the expected file in the top five, and a rebuilt
index matches the original (DESIGN 15, M3).

#### M3.1 The index

- **Depends on:** M2.4. **Design:** 10 (search).
- **Files:** `src/aft/index.py`, `tests/test_index.py`.
- **Behavior:** `~/.afterthought/index.sqlite` with FTS5 tables per collection: `pages`
  (active curated lines, with title and description; archive only as a separate
  collection), `digests`, `evidence`, `reports`, and later `library`. Rows carry path,
  project id, agent, session, time, and a heading or entry id. Updated incrementally by
  file content hash and evidence row id; `aft reindex` drops and rebuilds everything.
  BM25 weights title and description above body.
- **Tests:** incremental update after a page edit; reindex equals incremental result;
  superseded lines are not in `pages`.

#### M3.2 Search, read and trace

- **Depends on:** M3.1. **Design:** 10.
- **Files:** `src/aft/search.py`, `src/aft/cli.py` (`aft search`, `aft read`,
  `aft trace`), `tests/test_search.py`.
- **Behavior:** `aft search "<words>" [--project <slug>] [--archive] [--reports]
  [--json] [--limit 8]` prints one line per hit: path, one-line description, and a
  snippet with the match. It defaults to the current directory's project plus global
  pages, and `--all` searches every project. `aft read <path> --level outline|abstract|full`
  prints headings only, the description plus the active facts, or the whole file.
  `aft trace <page> <source-id>` prints the source's quote and, if the evidence still
  exists, the three entries around it; otherwise it says the full context has expired.
- **Tests:** a known-answer set of 20 queries over a fixture vault and archive, each with
  its expected file in the top five; the default excludes reports and the archive.
- **Review checkpoint:** yes (end of M3).

### M4 Dream

Done when: the fixture suite in DESIGN 15 (M4) passes.

#### M4.1 Scheduling, lock and hand-edit checkpoint

- **Depends on:** M2.6. **Design:** 9 (a run, step 1).
- **Files:** `src/aft/dream/__init__.py`, `dream/schedule.py`, `tests/test_dream_schedule.py`.
- **Behavior:** `aft dream` (also called at the end of `aft work`) exits at once if no
  digest is waiting. For each project with waiting digests and no session activity for
  `dream.quiet_minutes` (from the queue's last event time): if the vault has uncommitted
  changes modified within the quiet period, skip the project this run; older uncommitted
  changes are committed first as `checkpoint: human edits`, and lines Ted added are tagged
  `[stated]` with a source `resource: aft://edit/<commit>` whose quote is the line itself.
  Holds `vault.locked` for the whole run.
- **Tests:** no digests, no commit; recent hand edit, project skipped; older hand edit,
  checkpoint commit separate from the dream commit.

#### M4.2 Page writer

- **Depends on:** M0. **Design:** 6 (page format, fact state).
- **Files:** `src/aft/pagewrite.py`, `tests/test_pagewrite.py`.
- **Interface:** edit operations on a parsed page, rendered back to text: `add_fact`,
  `add_source`, `confirm(fact, source)`, `supersede(fact, new_fact, source)`,
  `withdraw(fact)`, `mark_stale(fact, reason)`, `add_callout(fact, type, text)`,
  `resolve_callout`, `add_candidate`, `update_counts`, `promote(candidate)`.
- **Behavior:** edits change only the lines they touch; everything else is preserved byte
  for byte. The result always parses and lints.
- **Tests:** each operation round-trips through `page.parse` and `lint`; untouched lines are
  unchanged.

#### M4.3 Retrieval and the decision call

- **Depends on:** M3.1, M4.2, M2.5. **Design:** 9 (a run, steps 2 and 3).
- **Files:** `src/aft/dream/decide.py`, `src/aft/prompts/decide.md`,
  `src/aft/prompts/decide.schema.json`, `tests/test_decide.py`.
- **Behavior:** for each verified fact from a waiting digest, search the project's pages
  (and global pages for global facts) for the ten most related active or candidate lines,
  number them `L1` to `L10`, and ask the model for one decision.
- **Prompt** (`src/aft/prompts/decide.md`):

```text
You maintain a project's long-term memory. You receive one new fact, with the quote that
supports it, and up to ten existing lines from memory, numbered L1 to L10. Decide what the
new fact means for memory. You only judge meaning; rules applied after you decide whether
anything changes.

Choose one action:
- add: the fact is new; no existing line says the same thing or contradicts it.
- confirm: an existing line already says the same thing. Name it.
- supersede: the fact replaces an existing line because the thing it describes has
  changed. Name the line.
- conflict: the fact contradicts an existing line, and it is not clear that the thing has
  changed. Name the line.
- skip: the fact is not worth keeping: it is routine, temporary, re-derivable from the code
  or git history, or too vague to act on.

Two lines say the same thing when an agent reading either would act the same way, even if
the wording differs. A fact about a different part of the system is new, not a conflict.

Give a reason of at most 200 characters that a person can check against the quote.
```

- **Schema:** `action` (enum), `line` (`L1` to `L10` or null; required for confirm,
  supersede and conflict), `reason` (max 200).
- **Tests:** with the fake endpoint, each action is carried to the rules with its target;
  a named line outside the list is rejected.
- **Review checkpoint:** yes (prompt and schema).

#### M4.4 The rules

- **Depends on:** M4.2. **Design:** 9 (the rules, authority to replace a fact).
- **Files:** `src/aft/dream/rules.py`, `tests/test_rules.py`.
- **Interface:** `check(decision, new_fact, target_line, context) -> Outcome`, where
  `Outcome` is one of `apply(action)`, `downgrade(to_action, rule)` or `reject(rule)`, and
  `context` carries the fact's origin (primary or worker session), dates, and recurrence
  counts. Pure functions only: no I/O.
- **Behavior:** R1 to R9 exactly as DESIGN 9 states them, with the authority table. In
  particular: `[inferred]` and `[suggested]` never replace a line; `[observed]` never
  replaces a `preference` or `decision`, and contradicting one produces `needs you`;
  replacing requires evidence newer than the target's date; an inferred fact stays a
  candidate until 3 signals across 2 primary sessions, with signals older than 30 days
  counting half; a worker-only fact stays a candidate; a global page changes only from
  `[stated]`.
- **Tests:** one test per cell of the authority table and per rule, including every
  refusal. This file is where most of the build's tests live.
- **Review checkpoint:** every PR touching this file.

#### M4.5 Apply, maintenance and commit

- **Depends on:** M4.1, M4.3, M4.4. **Design:** 9 (a run, steps 5 to 7).
- **Files:** `src/aft/dream/apply.py`, `dream/maintain.py`, `tests/test_apply.py`.
- **Behavior:** applies surviving outcomes through the page writer; creates
  `projects/<slug>/overview.md` and registers the project on its first curated fact;
  checks anchors with ripgrep against the default branch (skipping anchors whose commit is
  not yet on it); moves superseded lines older than 90 days to `archive/<slug>.md`; flags
  pages over 12,000 characters in the report; regenerates each `index.md` and appends to
  `log.md`; runs lint and aborts the run without committing if lint fails; marks consumed
  digests with `aft.consumed: <commit>`; makes one commit per project run.
- **Tests:** the M4 fixture suite from DESIGN 15, driven end to end with the fake endpoint.
- **Review checkpoint:** every PR touching `apply.py`.

#### M4.6 Report and needs-you

- **Depends on:** M4.5. **Design:** 9 (the report).
- **Files:** `src/aft/dream/report.py`, `tests/test_report.py`.
- **Behavior:** appends one section per run to `reports/<yyyy>/<mm>/<date>.md` in the fixed
  order from DESIGN 9, and regenerates `reports/needs-you.md`. Reports are indexed only in
  the `reports` collection and are never read by the dream.
- **Tests:** second run on a day appends; section order is fixed; needs-you lists every open
  conflict and question with links; the dream's inputs never include a report path.
- **Review checkpoint:** yes (end of M4).

### M5 Use and evaluate

#### M5.1 `aft eval`

- **Depends on:** M3.2, M1.2. **Design:** 15 (M5).
- **Files:** `src/aft/eval.py`, `tests/test_eval.py`, `planning/golden.example.yaml`.
- **Behavior:** reads a golden set, `~/afterthought/golden.yaml`, whose entries each have a
  `question`, a `project`, `expect` (paths or fact text that must be found), and
  `expect_not` (text that must not appear in the injection or top results). It checks the
  injection block and `aft search` for each, and reports pass or fail per entry. It never
  calls a model.
- **Tests:** a small golden set over a fixture vault, including negative cases.

M5 itself is two weeks of use on afterthought and one real project. Ted writes the golden
set; tuning of caps, prompts and budgets comes from what the reports and `aft eval` show.

### M6 Switch-over

- **M6.1 Import `MEMORY.md` files:** `aft import claude-memory` reads Claude Code's memory
  files and creates one digest per file with each line as an `[inferred]` fact whose
  quote is the line itself, so the normal rules decide what survives.
- **M6.2 and M6.3 are Ted's:** turning off Claude Code's automatic memory, and adding a
  memory section to `~/AGENTS.md`. The plan provides the text to add; workers do not edit
  Ted's settings or instruction files.

### M7 Library

Done when: the RV-C and Victron collections import with every file accounted for, every
reference quote verified, and no instruction file, tooling or git history retained; a
false claim planted in an imported index is rejected; known-answer queries about the RV-C
specification return the right document and page (DESIGN 15, M7).

- **M7.1 Survey:** `aft library survey <path>`: file list with type, size, hash; duplicates;
  suggested derived pairs; instruction files and tooling flagged; prints the procedure.
  Changes nothing.
- **M7.2 Manifest and validator:** the manifest format (YAML: `name`, `title`,
  `description`, `projects`, and `files` mapping each surveyed path to a role and
  relations) and a validator enforcing DESIGN 11: every file has one role, relations point
  inside the collection, instruction files and tooling are `exclude`.
- **M7.3 Import:** copy files, extract text (`pdftotext -layout` per page; markdown and
  text as they are; JSON split by top-level key), store extracted text by content hash in
  `~/.afterthought/library/`, add the `library` index collection, generate the reference
  pages' structure and contents map from the files, record hashes for `aft lint`, write
  the import report, commit.
- **M7.4 Imported maps as evidence:** check each claim taken from an existing index (path
  exists, heading or key exists, quoted description appears in the target document);
  failures go in the import report and are never used.
- **M7.5 The import skill:** the step-by-step procedure as a skill for Claude Code and pi,
  installed by `aft install claude` and `aft install pi`. It drives only the `aft library`
  commands.
- **M7.6 Facts from notes and instruction files:** the agent proposes facts with quotes;
  each is verified against its file and enters the dream as a candidate digest.

## 6. Ted's checklists at milestone ends

### 6.1 M1

1. Capture one real hook payload per event for Claude Code and pi into
   `tests/fixtures/hooks/` (M1.1 explains how).
2. Run `aft init`, `aft project add ~/src/afterthought`, and the M1.5 seed script against
   the real vault.
3. Run `aft install claude --dry-run`, check the diff, then install. Same for pi.
4. Start a new session in each agent in `~/src/afterthought` and check both show the same
   memory block.

### 6.2 M2

1. `aft install timer`. Set `capture.worker_paths` for firstmate's worktrees
   (`~/.treehouse`) and the validation gate (`~/.no-mistakes/worktrees`).
2. Work normally for a day, then read the digests in `~/afterthought/digests/` and check
   their facts and quotes.
3. Try `aft off` and `aft on` in a session and confirm nothing between them appears.

### 6.3 M4

1. Read the first week of dream reports and `needs-you.md`.
2. Revert one dream commit with `git revert` to confirm it works.

## 7. Order and parallel work

```text
M0 (built)
 ├─ M1.1 events ── M1.3 claude install
 │        └────── M1.4 pi extension
 ├─ M1.2 inject
 ├─ M1.5 seed pages
 ├─ M2.1 readers ─┬─ M2.2 queue (needs M1.1)
 │                └─ M2.3 filter ── M2.4 evidence ── M3.1 index ── M3.2 search
 ├─ M2.5 model ───── M2.6 digest (needs M2.2-M2.5, M1.2) ─┬─ M2.7 workers
 │                                                         ├─ M2.8 forget
 │                                                         └─ M2.9 timer
 ├─ M4.2 page writer ── M4.4 rules
 └─ after M2.6, M3.1, M4.2, M4.4: M4.1 ── M4.3 ── M4.5 ── M4.6
M5 after M3.2 and M4.6; M6 after M5; M7 after M3.2 and M4.6
```

The first wave can run in parallel: M1.1, M1.2, M1.5, M2.1, M2.5 and M4.2.
