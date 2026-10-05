# Afterthought build plan

This plan turns [DESIGN.md](DESIGN.md) into tasks that one worker can each complete alone.
Claude builds from it; the tasks merged before 2026-09-29 were built by firstmate's workers.
The design is the authority on behavior; this plan is the authority on sequence, interfaces and done
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
   and `uv run ruff format --check` pass on every PR. GitHub Actions runs all three on every
   pull request (`.github/workflows/ci.yml`), and a PR is green when that run passes.
5. **Tests never touch real state.** Every test runs with `AFT_HOME` and the vault in a
   temporary directory, a temporary git identity (see `tests/conftest.py`), and no network.
   Model calls go to the fake endpoint from M2.5.
6. **No new runtime dependencies** without asking. Today there is one: PyYAML. The model
   client uses the standard library.
7. **Reviews look at code and behavior** against the design section the task cites, never
   at the wording of planning documents.
8. **Size is measured, not a target.** Performance and accuracy come first. Count with
   `cat src/aft/*.py src/aft/**/*.py | grep -cvE '^\s*($|#)'` and report the number at each
   milestone end; remove real duplication when it is found, never working code for the
   count's sake.
9. **Never write to the real vault or `~/.afterthought`** during a task. Installing into
   the real environment (`aft install`, systemd units, hooks) is done by Ted, at milestone
   ends, from the checklists in section 6.
10. **One review round, then merge.** A PR gets one fix round from its gate review. Only
    blocking findings hold the merge: privacy (text reaching the archive, index or model
    that must not), data loss, the trust rules (R1 to R10, the authority table, quote
    verification), or a broken interface that other tasks build against. They are fixed in
    that round, and a second round only verifies those fixes. Every other finding is listed
    in the PR description and collected into one follow-up task per module, dispatched
    after the milestone's plan tasks. A PR merges when CI is green and no blocking finding
    is open. The section 3 checkpoints still run, and their findings are sorted the same way.

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
| `aft.cli` | `aft init`, `aft key`, `aft project list/add/rename/forget`, `aft lint`. New commands are added here as subcommands. A task whose entry names a command owns that command's wiring here, whether or not its file list names `src/aft/cli.py`; the wiring is argument parsing plus one call into the task's module. |

New code follows the same shape: small modules, dataclasses for records, pure functions
where possible, errors as exceptions caught once in `cli.main`.

## 3. Review checkpoints with `claude -p`

The PRs and milestones below get an independent review in a fresh `claude -p` session,
separate from the one that wrote the code:

| When | Why |
|---|---|
| The last PR of M1, M2, M3, M4, M7 | Milestone review against the design |
| Every PR touching `src/aft/dream/rules.py` or `src/aft/dream/apply.py` | The rules decide what becomes trusted memory |
| Every PR changing a model prompt or JSON schema | Prompts are where silent quality loss hides |

The builder runs, from the repository root:

```bash
git diff origin/main...HEAD > /tmp/aft-review.diff
{ cat planning/review-prompt.md
  printf '\nTask: %s\nDesign sections: %s\nThe diff is in /tmp/aft-review.diff.\n' \
    "<task id and title>" "<sections the task cites>"
} | claude -p --model claude-opus-5-5 --allowedTools "Read,Grep,Glob" > /tmp/aft-review.md
gh pr comment <pr> --body-file /tmp/aft-review.md
```

The builder then fixes every finding marked `blocking` in the same PR, or replies on the PR
explaining why a finding is wrong. Non-blocking findings are listed in the PR description
for Ted.

Each review spends Claude allowance. At roughly ten checkpoints plus the rules and prompt
  PRs, expect twenty to thirty reviews over the build.

## 4. When the builder is unsure

A builder that finds the design silent, contradictory, or wrong for the case in front of it
does not guess. It writes the question and the options it sees, and sorts it:

- **A design gap** is settled by the builder: the design is silent, the
  answer follows from the design's principles, and it changes nothing the design already
  states (what a reader skips, how a script behaves on a rerun, which date a fact keeps).
  The ruling and its reason go on the PR, and Ted reviews
  the milestone's rulings at its end. Rulings Ted accepts are folded into this plan.
- **A design change** goes to Ted: anything that contradicts the design or changes
  behavior it states, changes an interface other tasks depend on, adds or drops scope, adds
  a dependency, or touches the trust rules (R1 to R10, the authority table, quote
  verification). When it is unclear which kind a question is, it goes to Ted.

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
- **First step:** use real payloads, not documentation. The Claude Code 2.1.283 payloads
  were captured on 2026-09-28 and are in firstmate's
  `data/afterthought-m1-1-hook-events/captured-hooks/`, with a README noting the fields the
  documentation omits; copy them into `tests/fixtures/hooks/`. Parsers ignore unknown
  fields. pi has no shell hooks of its own, so its payloads are whatever the M1.4 extension
  sends, and the two tasks must agree on that shape.
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
  2. last session: `handoff` from the newest digest in `digests/<slug>/` (the highest part on the newest day) (empty until M2)
  3. active and conflict/needs-you facts from `projects/<slug>/overview.md` in the order
     State, Decisions, Lessons, Procedures, Facts, References; a conflict or question is
     marked `(disputed)` or `(waiting on Ted)` after the line; source tags and footnotes are
     stripped
  4. active and conflict/needs-you facts from `global/preferences.md`, marked the same way
     as in layer 3, since a preference stands until Ted changes it
  5. recent: the active lines of `projects/<slug>/recent.md`, marked as in layer 3, under
     `## Recent (last <dream.short_term_sessions> sessions, newest <date>)`, taking only
     the room layers 1 to 4 leave (F32)
  6. one line per other page in `projects/<slug>/` (not `recent.md`) and per reference
     collection whose `aft.projects` includes the project:
     `- <title>: <description> (aft read <path>)`
  7. `Search memory: aft search "<words>". Read a page: aft read <path>.`, then the marker
     line (F44).

  An unregistered or unresolved directory gets only layers 4 and 7. Candidates, stale,
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
  - on `session_before_compact`: re-run the hook as `compaction`, and refresh the kept
    block
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
  whose `quote` is copied from that section. The page passes `aft lint`. Run against a
  vault that already has the page, it refuses unless given `--overwrite`. With
  `--overwrite`, a fact whose statement and quote are unchanged keeps its earlier date,
  because re-reading the same source is not new evidence; a new or changed fact gets
  today's date.
- **Tests:** the script's output lints clean; every quote appears in `DESIGN.md`; a rerun
  refuses without `--overwrite`; with it, unchanged facts keep their dates.

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
  `isMeta` entries, thinking blocks, the summary entries it writes on compaction, and the
  failure notices Claude Code writes as assistant entries (model `<synthetic>` or
  `isApiErrorMessage: true`; match on those markers, never on the message text), since
  they are infrastructure noise, not anything the agent said; split tool results out of
  user messages. pi: read `message` entries, follow the active
  branch only. Unknown entry types are skipped, never fatal, but counted: a file that has
  content yet yields no recognized entry raises `UnrecognizedTranscript`, so a changed
  transcript format is reported instead of reading as an empty session. Capture logs it,
  names it in the next report, and keeps the job for a retry after the reader is fixed.
  An unreadable file or malformed line is reported as a gap tied to the entry that
  produced it, with a count for the active branch. A session holding only assistant text
  and recognized harness notices yields no entries, because an assistant line never
  becomes a fact's subject on its own.
- **Tests:** fixture transcripts (synthetic, never copied from real sessions) for each
  agent, including a compaction, a tool error and a resumed session, plus the gap report
  and the assistant-only session.

#### M2.2 Capture queue, capture hook and sweep

- **Depends on:** M1.1, M2.1. **Design:** 8 (capture queue).
- **Files:** `src/aft/queue.py`, `src/aft/capture.py`, `src/aft/cli.py` (wiring the
  `compaction` and `session_end` events of `aft hook` to `enqueue`), `tests/test_queue.py`.
- **Interface:** `enqueue(config, event) -> None`; `next_job(config) -> Job | None`;
  `complete(config, job, watermark, *, off_record)`, where `off_record` is required; `fail(config, job, error)`; `sweep(config, now)`.
- **Behavior:** the queue is `~/.afterthought/queue.sqlite` in WAL mode. `enqueue` writes one
  row inside `BEGIN IMMEDIATE` and returns; it never calls a model and never reads the
  transcript. A session has one watermark (the last digested entry id), updated only when
  its digest is written. `sweep` scans both agents' session directories for sessions
  modified in the last 7 days and compares each against its recorded watermark, not against
  the time of the last sweep, so a session whose hook never fired is found whatever the
  timer cadence. It enqueues any with no pending job that has new entries past its
  watermark and is idle for 30 minutes, or has grown by 200 KB since its watermark. The
  queue also stores, per session, whether an off-the-record span is still open at the
  watermark (see M2.3). Resolution to a project
  happens at digest time, not in the hook.
- **Tests:** two processes enqueue concurrently without loss; a crash between reading and
  completing leaves the job retryable; the hook path completes in under 50 ms with a
  1,000-row queue; the sweep finds a session whose hook never fired.

#### M2.3 Filter, off the record, and redaction

- **Depends on:** M2.1. **Design:** 8 (filter), 12.
- **Files:** `src/aft/filter.py`, `src/aft/redact.py`, `tests/test_filter.py`.
- **Interface:** `filter_entries(entries, config, off_record: bool = False) ->
  tuple[list[Entry], bool]`, taking whether a span is open at the start of the chunk and
  returning the kept entries and whether a span is still open at its end;
  `redact(text, patterns) -> str`.
- **Behavior:** in this order: drop off-the-record spans (a user entry whose text begins,
  after whitespace, with `aft off` or `off the record`, case-insensitive, trailing
  punctuation ignored, starts a span; `aft on` or `on the record` ends it; the marker
  entries themselves are dropped; a span still open at the end of a chunk carries into the
  next chunk of the same session through the queue, so a boundary never ends it); keep
  user and assistant text; from a tool entry the agent flagged as an error, keep its first
  500 characters plus any matching lines below; from other tool entries keep only lines
  matching error patterns (`error`, `failed`, `Traceback`, `exception`,
  `FAILED`, non-zero exit) and `path:line` references; cap each entry at 1,500 characters
  on a word boundary with ` [...]`; redact. Built-in redaction covers private key blocks,
  `sk-`/`ghp_`/`github_pat_`/`xox[bp]-`/AWS key ids, `Authorization:` headers,
  and credentials in URLs, plus `capture.redact_patterns` (F43).
  Redacted text becomes `[redacted]`.
- **Tests:** each marker form; a marker mid-sentence does nothing; an unclosed span runs to
  the end; a span opened in one chunk stays closed to capture in the next; each secret
  form; the cap; error-line extraction; a flagged entry with no matching line keeps its
  first 500 characters.

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
- **Credentials:** add `api_key_env` (default empty) to `ModelConfig` in `aft/config.py` and
  to the commented default config. It names an environment variable; `config.toml` never
  holds the key itself. When `api_key_env` is set, the client reads that variable at call
  time and sends it as `Authorization: Bearer <key>`; if the variable is unset or empty, the
  call fails with a `ModelError` naming the variable (never its value). When `api_key_env`
  is empty, no header is sent. The key never appears in logs, error messages or reports.
- **Tests:** against the fake server: valid response; invalid then valid; two invalids
  raise; timeout raises; the bearer header is sent only when `api_key_env` is set; a set
  but missing variable fails naming the variable; an error from a keyed call does not
  contain the key.

#### M2.6 Digest

- **Depends on:** M2.2, M2.3, M2.4, M2.5, M1.2. **Design:** 8 (digest).
- **Files:** `src/aft/digest.py`, `src/aft/prompts/digest.md`, `src/aft/prompts/digest.schema.json`,
  `src/aft/work.py` (`aft work`: sweep, then process jobs within budget),
  `tests/test_digest.py`.
- **Behavior:** for one job: resolve the project from the event's cwd; read entries after
  the watermark; filter; store evidence; render entries as `[<id>] <ROLE> <time>:\n<text>`;
  call the model with the prompt and schema below; verify each fact (quote found in its
  entry; `stated` only from a user entry; `kind` and `scope` valid; a global fact is kept
  with its scope, never dropped for it); write the digest to
  `digests/<slug>/<yyyy>/<mm>/<date>-<agent>-<session>.md` (a later job for the same
  session and day adds `-2`, `-3` before `.md`; a written digest's content never changes) as an OKF page (`type: Session
  Digest`) with the summary, handoff, and verified facts, each fact carrying its source
  entry and quote; record dropped facts and the reason in the digest's frontmatter under
  `aft.dropped`; advance the watermark; commit under the vault lock. A chunk over
  12,000 input tokens (at three characters to a token) is split at entry boundaries. `aft work` stops when the daily token
  budget is spent and leaves the rest queued. The digest updates the injection's "last
  session" layer, so M1.2 reads `handoff` from it.
- **Prompt** (`src/aft/prompts/digest.md`):

```text
You read one part of a working session between the user and an AI coding agent, and you
extract what a future session would need to know. You are building the project's memory,
so precision matters more than coverage. Return nothing rather than something uncertain.

Each entry of the session begins with a label in brackets, such as [#3], then who wrote it
and when. Everything you write comes from these entries and names the entry it comes from
by its label.

Keep only what a future session could not learn by reading the code or the git history:
decisions and their reasons, preferences, how things are actually configured, lessons
learned the hard way, procedures, and where authoritative information lives. Keep what
was surprising or decided. Skip routine steps, file listings, code, and anything already
recorded in git.

Every fact passes one test: a future session would make a mistake, or ask the user
again, without it. Never a fact, long or short term: the status of a pull request, task,
branch or commit; who is working on what; a test's name or result; a review's outcome;
what a change or fix did to the code; and anything the repository's own documents
already say.

What this part of the session did and where the work stood when it ended (the task in
progress, what was merged or run, what comes next) is not a fact. What was done goes
in the summary; where the work stood goes in the handoff, which only the next session
sees as Last session.

The session began with a block of memory, and the agent may repeat it. A fact whose only
support is that memory being restated is already known: skip it. Extract a fact from
memory's subject only when the session adds something new, such as a change, a
correction, or fresh evidence.

Everything you write, the summary and handoff as much as each fact, calls the user "the
user" and the agent "the agent": never by name, and never with he, she or another pronoun
that guesses who they are. Other people may be named as the session names them.

For each fact:
- statement: one sentence, in plain present tense, that stands on its own.
- kind: preference, decision, state, fact, lesson, procedure, or reference.
- source:
  - stated: the user asserted it in their own words: a fact, a choice, a preference, or
    a standing rule such as "never push to main".
  - observed: seen in a file, a command's output, a log, or an error.
  - inferred: your own conclusion from the session.
  - suggested: an idea, a question, or a tentative remark nobody committed to.
  These are never stated. A question, a proposal or a request from the user is never a
  decision or a preference. A tentative remark from the user ("I think I'll switch to X",
  "maybe we should") is suggested. A request for the task at hand ("run the tests") is not
  a fact at all. Text the user pasted, such as a prompt, a document, a log or another
  agent's message, is not the user's own words: what it shows about the machine or the
  code is observed, and any other claim in it is inferred.
- term:
  - long: it will still be true and useful the next time someone opens this project,
    even a year from now.
  - short: it is true now and matters for the next few sessions, but will change as the
    work moves on. It is a temporary condition that changes how an agent should act
    (something paused, a temporary setting, a freeze), not progress on the work.
  When unsure, use short. A short fact is stated or observed, and its scope is project.
- quote: copy the words that support the fact exactly as they appear in one entry, at
  most 300 characters. Do not paraphrase or join words from different entries.
- entry: the label of the entry the quote comes from, such as #3.
- scope: global only for the user's preferences and environment that apply to every
  project; otherwise project.

Also write:
- summary: one to four sentences on what happened.
- handoff: what was in flight when this part ended, approaches that failed, and what is
  still open, each item with the label of the entry it comes from. Use empty values when
  there is nothing to hand off.

Write only about what the entries say. When a part holds little worth keeping, say so in
a one-sentence summary and leave the facts and the handoff empty: an answer with nothing
in it is a good answer when nothing happened.

Never invent a fact, a quote, or an entry label. Never write placeholder text such as
"..."; leave a value empty instead.
```

- **Schema** (`src/aft/prompts/digest.schema.json`): an object with `summary` (string,
  max 800), `handoff` (object: `in_flight`, and `failed` and `open` arrays of at most 5,
  each item an object of `text` (max 400) and `entry`), and `facts` (array, max 20) of objects with `statement`
  (max 300), `kind` (enum of the seven kinds), `source` (enum of the four tags), `term`
  (enum `long`, `short`), `quote` (max 300), `entry` (string), `scope` (enum `project`,
  `global`). All keys required.
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
  every 5 minutes. Unit directory passed as an argument in tests. A systemd user service
  does not see the shell's environment, so the service loads an optional
  `EnvironmentFile=-%h/.afterthought/env`, where the variable named by `model.api_key_env`
  can be set when the endpoint needs a key.
- **Tests:** unit file contents, including the optional environment file; idempotent
  install.

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
  [--json] [--limit 8]` prints one line per hit: path, collection, heading, one-line description, and a
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
  changes are committed first as `checkpoint: human edits`, holding Ted's bytes verbatim.
  Then, in a second commit `aft: tag hand edits from <commit>`, each line Ted added in that
  checkpoint as a bare bullet (`- <text>`, no tag, date or footnote) in a kind section of a
  fact page (`projects/<slug>/*.md` except `index.md`, and `global/*.md`) is rewritten as
  `- <text> [stated] <date> [^id]`, dated with the checkpoint commit's UTC date, with a
  source `resource: aft://edit/<commit>` whose quote is the text. A line Ted typed or edited
  in full fact-line form keeps exactly what Ted wrote. The vault-wide check for recent edits
  skips every project for the run.
  Holds `vault.locked` for the whole run.
- **Tests:** no digests, no commit; recent hand edit, project skipped; older hand edit,
  checkpoint commit separate from the dream commit.

#### M4.2 Page writer

- **Depends on:** M0. **Design:** 6 (page format, fact state).
- **Files:** `src/aft/pagewrite.py`, `tests/test_pagewrite.py`.
- **Interface:** a `PageDoc` read from a path, with edit operations rendered back to text:
  `add_source(resource, title, quote) -> id`, `add_fact`, `add_candidate(statement, tag,
  date, refs)`, `confirm(fact, source, date)`, `update_counts(candidate)`,
  `supersede(fact, ..., tag, date)`, `withdraw(fact, date)`, `mark_stale(fact, reason,
  date)`, `add_callout(fact, type, text, date)`, `resolve_callout(fact)`,
  `promote(candidate, kind)`, and `fact(statement)` to look a fact up by its statement
  (an error unless exactly one fact matches). The dates, tag and kind are required because
  the grammar records them; M4.3 to M4.5 build against these signatures.
- **Behavior:** edits change only the lines they touch; everything else is preserved byte
  for byte. The result always parses and lints. Rulings (DESIGN 6):
  - Never lose text the tool did not write. An operation on a line or note that carries text
    outside the grammar (a hand-typed tail, an extra note row) refuses with a reason instead
    of rebuilding the line, and an edit may never make a parse error disappear by deleting
    the bytes it flagged. `resolve_callout` moves the whole note, header and every row, to
    `archive/<page path>` (the page's own vault path under `archive/`, so
    `global/preferences.md` archives to `archive/global/preferences.md`) with the date and the fact it belonged to, and never deletes it.
  - `add_callout` refuses a note whose text cites a `[^id]` missing from the page's
    sources; `aft lint` checks note footnotes the same way (on main since M0).
  - Statements are checked by round trip, not by pattern: a statement is accepted when the
    written line parses back to exactly that statement. The look-alike tail guard is
    removed; the refusals for blank text, `~~` and line breaks stay.
- **Tests:** each operation round-trips through `page.parse` and `lint`; untouched lines are
  unchanged; a hand-typed tail or note row makes the operation refuse and leaves the page
  unchanged; `resolve_callout` archives the note verbatim; a statement ending in text that
  looks like a tag is written and parses back unchanged.

#### M4.3 Retrieval and the decision call

- **Depends on:** M3.1, M4.2, M2.5. **Design:** 9 (a run, steps 2 and 3).
- **Files:** `src/aft/dream/decide.py`, `src/aft/prompts/decide.md`,
  `src/aft/prompts/decide.schema.json`, `tests/test_decide.py`.
- **Behavior:** for each verified fact from a waiting digest, search the project's pages
  for a project fact, or only the global pages for a global fact (R6 lets a global fact edit
  nothing else; reference pages scoped to several projects are outside v1), for the ten most related active or candidate lines,
  number them `L1` to `L10`, and ask the model for one decision.
- **Prompt** (`src/aft/prompts/decide.md`):

```text
You maintain a project's memory. You receive one new fact, with the quote that supports
it, and up to ten existing lines from memory, numbered L1 to L10. Decide what the new fact
means for memory. You only judge meaning; rules applied after you decide whether anything
changes.

Memory has two terms. A long-term fact should still be true and useful a year from now. A
short-term fact is a temporary condition that changes how an agent should act (something
paused, a temporary setting, a freeze), not progress on the work, and it is meant to change. The new fact says which term it is, and an existing line marked recent is
short-term memory.

A fact earns its place in memory only when a future session would make a mistake, or ask
the user again, without it. Never a fact, long or short term: the status of a pull
request, task, branch or commit; who is working on what; a test's name or result; a
review's outcome; what a change or fix did to the code; and anything the repository's own
documents already say. A question, a proposal or a request from the user is never a
decision or a preference.

Choose one action:
- add: the fact is new; no existing line says the same thing or contradicts it.
- confirm: an existing line already says the same thing. Name it.
- supersede: the fact replaces an existing line because the thing it describes has
  changed. A new fact that changes how something is done, which an existing line
  describes, supersedes that line rather than adding beside it. Name the line.
- conflict: the fact and an existing line say different things about the same thing, so
  both cannot be true, and it is not clear that the thing has changed. Name the line.
- skip: the fact is not worth keeping: it fails that test, is on the never-a-fact list
  above, or it is routine, re-derivable from the code or git history, or too vague to act
  on. A long-term fact that will not stay true is not worth keeping either. A short-term
  fact is meant to be temporary, so that alone is no reason to skip it.

Two lines say the same thing when an agent reading either would act the same way, even if
the wording differs. Before naming a line for supersede or conflict, check that it is about
the same thing as the new fact. A fact about something else, or one that can be true at the
same time as the line, is not a supersede or a conflict: choose confirm if a line already
says the same thing, and add otherwise.

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
- **Behavior:** R1 to R10 exactly as DESIGN 9 states them, with the authority table. In
  particular: `[inferred]` and `[suggested]` never replace a line; `[observed]` never
  replaces a `preference` or `decision`, and contradicting one produces `needs you`;
  replacing requires evidence newer than the target's date; an inferred fact stays a
  candidate until 3 signals across 2 primary sessions, with signals older than 30 days
  counting half; a worker-only fact stays a candidate, and a `confirm` from a worker session is skipped
  and reported (no source, no date); a global page changes only from
  `[stated]`, and a global fact that is not `[stated]` is skipped and reported. A global
  `preference` goes to `global/preferences.md`; every other global kind goes to
  `global/environment.md`.
- **Tests:** one test per cell of the authority table and per rule, including every
  refusal. This file is where most of the build's tests live.
- **Review checkpoint:** every PR touching this file.

#### M4.5 Apply, maintenance and commit

- **Depends on:** M4.1, M4.3, M4.4. **Design:** 9 (a run, steps 5 to 7).
- **Files:** `src/aft/dream/apply.py`, `dream/maintain.py`, `tests/test_apply.py`.
- **Behavior:** applies surviving outcomes through the page writer; creates
  `projects/<slug>/overview.md` and registers the project on its first curated fact;
  checks anchors with ripgrep against the default branch (skipping anchors whose commit is
  not yet on it), marking a broken anchor stale and clearing the mark when the words appear
  again, except on a line with an open note, where the break is only reported; moves superseded lines older than 90 days to `archive/<page path>`, the page's own vault path under `archive/`; flags
  pages over 12,000 characters in the report; regenerates each `index.md` and appends to
  `log.md`; runs lint and aborts the run without committing if lint fails; marks consumed
  digests with `aft.consumed: <run id>`, the dream run's id, which the run's commit message
  also names; makes one commit per project run.
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

Done when: the RV-C collection (imported as `authoritative`) and the Victron collection
(imported as `reference`) import with every file accounted for, every reference quote
verified, and no instruction file, tooling or git history retained; a false claim planted
in an imported index is rejected; facts from the RV-C specification enter as `[observed]`
and facts from the Victron collection as candidates; known-answer queries about the RV-C
specification return the right document and page (DESIGN 15, M7).

- **M7.1 Survey:** `aft library survey <path>`: file list with type, size, hash; duplicates;
  suggested derived pairs; instruction files and tooling flagged; prints the procedure.
  Changes nothing.
- **M7.2 Manifest and validator:** the manifest format (YAML: `name`, `title`,
  `description`, `projects`, `authority` (`authoritative` or `reference`, default
  `reference`; see F5), and `files` mapping each surveyed path to a role and
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
  each is verified against its file and enters the dream as a digest, tagged by the
  collection's authority (DESIGN 11, Authority; F5). A library quote is never the user's
  own words, so nothing imported is `[stated]`.

### Follow-ups from rulings

Rulings made after a task merged. Each is a small task of its own, briefed like the others.

- **F1 Seed overwrite keeps curated text** (after M1.5). `--overwrite` refuses when the page
  holds anything the seed did not write: a hand-written line, an extra tag or anchor, or a
  state mark on a seeded line. The seed is a one-time bootstrap; after that the page is
  curated memory. Test: each kind of curated edit makes `--overwrite` refuse.
- **F2 Rejected lines stay visible** (after M0 and PR 6). `page.Page` gains `rejected: list`
  of `(line, raw text, reason)` for fact lines the parser refused. They are never facts, so
  memory consumers ignore them, but lint and the seed script can see them. Additive: nothing
  existing changes. Tests: a refused line appears in `rejected` and not in `facts`.
- **F3 Filter carries off-the-record across chunks and keeps flagged errors** (after M2.3,
  with M2.2). Implements the M2.3 interface and behavior above. No queue file predates
  this, so a queue file without per-session span state is refused, not migrated.
- **F4 Clear stale** (after M4.2). `PageDoc.clear_stale(fact, date)` removes a stale mark;
  M4.5 uses it when an anchor's words reappear; the line keeps its date. Trust decisions
  such as whether a replacement is newer live in `aft.dream.rules`; the writer only writes. Test: a cleared line parses as active and
  lints.
- **F8 Forget is complete** (after M2.8; M2 milestone review). DESIGN 12. `aft forget session`
  also clears the session from the capture queue (pending and failed jobs, their error text,
  its digest debt and session row), so a later `aft work` cannot bring its words back, and
  scrubs its quotes in reports and `needs-you.md`. `aft forget pattern` replaces matches with
  `[redacted]` in digest quotes, page sources and statements, and reports, and updates the
  index, as well as the archive. `aft redigest` applies the current redaction patterns and
  counts against the daily budget (DESIGN 8). Tests: forget, then `aft work`, leaves no trace;
  after forget pattern, `aft search` finds the pattern nowhere; a redigest of a session whose
  text matches a pattern added later sends none of it to the model.
- **F9 Capture privacy** (after M2.7; M2 milestone review). DESIGN 8 and 12. The retention
  purge runs from `aft work`; redaction covers quoted JSON keys and the missing token formats
  and runs before the length cap; sessions with fewer than two prompts are skipped;
  `capture.exclude_projects` names projects that are never captured. Test: a secret planted
  in a transcript is absent from the model request, the archive and the index after
  `aft work`.
- **F10 Archived notes are searchable** (after M4.2; ruling 2026-09-30). DESIGN 10. An
  archive file written by `resolve_callout` has a section heading so it parses, and the
  archive collection indexes resolved notes with the fact they belonged to. Test: after a
  note is resolved, `aft search --archive <words of the answer>` finds it, and default
  search does not.
- **F20 `--archive` reaches lines still on their page** (ruling 2026-09-30; M3 review 11).
  DESIGN 10. The archive collection also indexes superseded and stale lines that sit on live
  pages, so a line superseded today is found with `aft search --archive` and never by
  default search. Withdrawn lines stay out of every collection. Tests: a line superseded in
  this run and a stale line are found with `--archive` and not without it; a withdrawn line
  is found by neither; the F18 pipeline test's xfail becomes a passing case.
- **F21 Injection holds its budget on a real vault** (M1 milestone review 1). DESIGN 10.
  `aft inject`, and the session-start hook that prints the same block, finish in under 150 ms
  end to end (process start included) on a vault of 50 pages of 200 facts, each fact with a
  source as R1 requires, plus 20 digests on the newest day. A page's title, description and
  timestamps are read without parsing its `sources` list, and the CLI imports only what the
  command needs. Tests: the benchmark uses pages with real sources and measures a cold
  subprocess; the block's text is unchanged. The PR body gives timings before and after.
- **F22 What the block contains** (M1 review 3, 4, 11). "Last session" takes the newest digest
  of a primary session that carries a handoff; worker, library and memory-import digests never
  supply or remove it. Layers are trimmed in the order M1.2 gives, so global preferences are
  kept ahead of page pointers. A working directory that no longer exists still gets global
  preferences and the how-to lines. A `SessionStart` with no transcript path still injects.
- **F23 Hooks stay inside their budget** (M1 review 6, 7, 8). A hook returns within its 2
  second budget even when another process holds the queue's write lock (the SQLite busy
  timeout on the hook path sits inside the budget, and the failure is logged). The hook's
  timeout is never swallowed as a missing file. A run with `AFT_HOME` set to a directory that
  is not the default never calls the user's systemd.
- **F24 The pi extension and packaging** (M1 review 5, 9, 10). After a compaction the pi
  extension asks for a fresh block and replaces the one it holds, as Claude Code's
  `SessionStart` does. Building the payload cannot throw out of a handler. The pi fixtures and
  the adapter match the installed pi's payloads. The wheel ships `integrations/`, so
  `aft install claude` and `aft install pi` work from an installed package; a test builds the
  wheel and installs from it into a temporary directory.
- **F25 The real vault refuses a temporary project** (ruling 2026-09-30). When `AFT_HOME` is
  not set, so the command is using the default home and the user's real vault,
  `aft project add` refuses a path under the system temporary directory, and says to set
  `AFT_HOME` for a scratch run or pass `--allow-temp`. With `AFT_HOME` set, nothing changes,
  so tests and benchmarks that use a temporary home are unaffected. Tests: the refusal and
  its message; `--allow-temp` registers; a run with `AFT_HOME` set registers a temporary path
  without the flag.
- **F26 A session is filed under the project it started in** (ruling 2026-09-30; tester
  finding T8). DESIGN 7. The session-start hook records `(agent, session, project id, slug)`
  in the queue after printing the block, skipping excluded sessions and directories that
  resolve to no project. At digest time a directory that no longer resolves takes the
  recorded project; with no record the session is archived and indexed under no project and
  the job closes without a model call; with `capture.exclude_projects` set and no record the
  job is dropped. `aft forget session` removes the record. Tests: each of those cases, and a
  busy queue costing a starting session neither its block nor its budget.
- **F27 Installing the timer enables it** (tester finding T7). `aft install timer` writes the
  units, reloads systemd, and enables and starts `aft-work.timer`, every time it runs, and
  says so; `--dry-run` names the commands; `--uninstall` disables and stops the timer before
  removing its unit. Units written to a directory systemd does not read run no command, and
  a refusal from systemd exits 1 with the commands to run by hand.
- **F28 An endpoint's own request fields** (ruling 2026-09-30; tester finding T10).
  DESIGN 13. `model.extra_body`, a table in `config.toml`, is merged into every request body;
  naming `model`, `messages`, `max_tokens` or `response_format` is a config error. An answer
  with no content and `finish_reason: length` fails once, without the retry, with an error
  that names the setting. Tests: the fields reach the endpoint; the reserved names are
  refused; the lost-to-reasoning error and its single call.
- **F29 Memory reflected back is not evidence** (ruling 2026-09-30; finding T14). DESIGN 8.
  Before a chunk is archived or sent to the model, every line that repeats a line of the
  project's memory block (list and quote markers and whitespace within the line ignored,
  lines of 20 characters or more) is taken out, and an entry left empty is dropped;
  worker archiving and `aft redigest` do the same. The digest prompt tells the model a fact supported only by restated memory is
  already known. Tests: the echo reaches neither the model nor the archive, the rest of the
  entry does, and the prompt says so.
- **F30 What the digest calls stated, and the term of each fact** (ruling 2026-09-30; the
  first real dream). DESIGN 6 (source tags; short-term and long-term memory), 8 (the digest).
  `[stated]` is an assertion in the user's own words; a request for the task at hand, a
  question and pasted text are not. What the session did goes in the summary and handoff,
  not in facts. Every fact carries `term: long | short`. The schema adds `term` (enum
  `long`, `short`, required); the script drops a short fact that is `inferred` or
  `suggested`, or whose scope is `global`, with the reason under `aft.dropped`; the digest
  page records each fact's term, and a fact with none (library and memory-import digests,
  and digests written before this task) is read as `long`. M2.6 holds the prompt's text.

  Tests: with the fake endpoint, a short `inferred` fact, a short `suggested` fact and a
  short global fact are dropped with their reasons; a long and a short fact are both written
  with their terms; a digest page without terms reads as long; the schema requires `term`.
  Review checkpoint: yes (prompt and schema). Once merged, Ted runs `aft redigest` on the
  sessions digested since the timer started, before anything dreams them.
- **F31 Short-term memory in the dream** (ruling 2026-09-30). DESIGN 6 (short-term and
  long-term memory), 9 (a run, R10, the report). Depends on F30. `config.toml` gains
  `dream.short_term_sessions` (integer, default 5; below 1 is a config error), shown in the
  config template. `projects/<slug>/recent.md` is a reserved fact page: lint refuses a
  Candidates section or an `[inferred]` or `[suggested]` line on it. The decision call's
  related lines include `recent.md`'s active lines, each marked `(recent)` in the numbered
  list, and `decide.md` says whether the new fact is long or short term; its `skip` becomes
  "routine, re-derivable from the code or git history, or too vague to act on, and for a
  long-term fact, not durable". `rules.py` routes a short fact as DESIGN 9 states (an `add`,
  or a `supersede` or `conflict` naming a long-term line, becomes an add on `recent.md`; a
  long-term line is only ever confirmed) with R4
  applied on `recent.md` as anywhere else, and a short fact from a worker session is skipped
  and reported. `apply.py` creates `recent.md` on its first line, promotes a line confirmed
  by a long-term fact, and in maintenance promotes lines whose sources are at least
  `short_term_sessions` sessions apart (each through the decision call as a long-term fact
  carrying all its sources) and then removes every line whose newest source has left the
  window. A project's sessions are its primary sessions with a digest, in the order the dream
  processes them; a source from a hand edit counts as the newest session dreamed before the
  edit's checkpoint commit. The report gains its Short term section (added, promoted, rolled
  off) before Revert. Tests: the DESIGN 15 M4 cases for short term; a rolled-off line is
  still found by `aft search` in its digest; a run with no new session for the project
  removes nothing; a promotion whose decision is `skip` leaves `recent.md` and is reported.
  Review checkpoint: yes (`rules.py`, `apply.py`, the decide prompt).
- **F32 Short-term memory in the block** (ruling 2026-09-30). DESIGN 10. Depends on F31.
  Injection gains the recent layer after global preferences: `recent.md`'s active lines,
  marked like layer 3, under a heading naming how many sessions they cover and the date of
  the newest. `recent.md` gets no page-pointer line. Tests: layer order; a vault with no
  `recent.md` builds the same block as before; the long-term layers keep their space when
  `recent.md` alone would fill the cap; the F21 benchmark still holds.

  F30, F31 and F32 land in that order. The timer stays off until F31 merges, since a dream
  before it would file short-term facts as long-term.
- **F33 User documentation** (ruling 2026-09-30; after F32). DESIGN.md is written for
  whoever builds afterthought; a user needs pages written for using it. `README.md` says
  what afterthought is and why, how to install it, a quick start (`aft init`, `aft project
  add`, `aft install claude` and `pi`, `aft install timer`, opening the vault in Obsidian),
  one paragraph on how it works, and links to `docs/`. `docs/concepts.md` explains, in plain
  language, evidence, digests and curated facts, the source tags, short-term and long-term
  memory, and what a user sees day to day. `docs/configuration.md` gives every
  `config.toml` key with its default and when to change it. `docs/commands.md` gives every
  `aft` command and flag. `docs/vault.md` covers the vault's layout, reading and editing
  pages in Obsidian, `reports/needs-you.md`, reverting a dream, and forgetting. The docs
  describe behavior and link to DESIGN.md for the reasons; they never restate a rule
  differently from the design. Tests: every key in the default config template appears in
  `docs/configuration.md`, and every `aft` subcommand appears in `docs/commands.md`, so a
  new setting or command without documentation fails CI.
- **F34 An answer must match its chunk** (ruling 2026-10-01; finding T17). DESIGN 8. The
  model is shown each entry under a label (`#1`, `#2`) that the script maps back to the
  entry's id, so a uuid copied wrongly no longer loses a fact. An answer with a fact citing
  an entry the chunk does not hold, or with a placeholder (no word in it) for a summary, a
  handoff item or a fact, is asked for once more and then fails the job. Each handoff item
  names its entry, and an item naming none of the chunk's is left off the page. Chunks are
  12,000 tokens at three characters to a token, and the answer's room is 8,000 tokens.
  Tests: a fabricated entry refuses the answer, a second good answer is kept, each kind of
  placeholder refuses it, the model sees labels and the page records ids, an ungrounded
  handoff item is left out, and every call stays under the cap. Review checkpoint: yes
  (prompt and schema).
- **F35 Sessions of no project are digested unfiled** (ruling 2026-10-01). DESIGN 7. A
  session whose directory resolves to no project, or is gone with no record, is archived
  under no project and digested to `digests/_unfiled/` (titled "Unfiled") instead of
  failing; `aft redigest` files such a session there too. Dreaming the `_unfiled` digests
  puts only global facts to the decision; each project fact is skipped and reported, no
  project is registered, and no `projects/_unfiled/` is written. An event in such a
  directory counts as `_unfiled` activity for the quiet period. Tests: a directory of no
  project and a deleted one are digested unfiled with no failure; an unfiled dream adds the
  global fact and skips the project fact; redigest files unfiled; the quiet period sees
  unfiled activity.
- **F36 A refused part costs only itself** (ruling 2026-10-01). DESIGN 8. A part whose
  answer is refused twice (by the schema or by F34's match) gives the page no summary,
  handoff or fact, and is recorded under `aft.refused_parts` (`first`, `last`, `reason`);
  the job's other parts are kept and the watermark moves. A refused last part leaves the
  handoff empty. A job whose every part is refused fails as before. `aft redigest` does the
  same. Tests: a refused first part, a refused last part, every part refused, and a
  redigest with one refused part.
- **F37 An answer about another session is refused** (ruling 2026-10-01; finding T17). DESIGN 8.
  An answer with two or more facts, every one dropped because its quote is not in the entry
  it cites, is refused like any answer that does not match its chunk: asked once more, then
  the part is refused (F36). Its summary and handoff carry no quote and came from the same
  reading. One missing quote, or facts dropped for another reason, only drop those facts.
  The prompt tells the model to write only about what the entries say, and that an empty
  answer is a good one when nothing happened. A check that the summary's words appear in the
  chunk was measured on real digests and refused: honest summaries scored as low as 0.35 of
  their words found, below an invented one's 0.60. Tests: an invented answer is refused, a
  real one after it is kept, one unquoted fact and a misattributed `[stated]` refuse nothing,
  and the prompt's words. Review checkpoint: yes (prompt).
- **F38 Pause and resume** (ruling 2026-10-01; finding T19). `aft pause [reason]` writes a
  marker, `~/.afterthought/paused`, with its time and reason; an existing pause is kept as
  it was. While it stands, `aft work` and `aft dream` print the pause and exit 0, whatever
  started them, unless given `--force`, and a session's end does not start the work service.
  Capture and injection are untouched. `aft resume` removes the marker and starts a run,
  past the five-minute trigger limit. A marker that cannot be read still pauses. Tests: each
  entry point respects the pause, `--force` runs once and keeps it, capture still queues,
  resume ends it and starts a run.
- **F39 The dream keeps to the daily budget** (ruling 2026-10-01; finding T20). DESIGN 13.
  Before each project, a dream run checks the day's token use against
  `work.daily_token_budget`; once it is spent, the project's digests wait for the next day
  and `aft dream` says so. Tests: a spent budget leaves digests waiting with no model call;
  a budget with room runs.
- **F40 Resetting the budget** (ruling 2026-10-02). DESIGN 13. `aft budget` prints today's
  use against `work.daily_token_budget` and what is left; `aft budget reset` writes the
  moment to `~/.afterthought/budget-reset`, and the day's count runs from midnight UTC or
  that moment, whichever is later. `usage.jsonl` is never changed. `aft work` and `aft dream`
  say when a reset is in effect. Tests: a reset clears the count and keeps the ledger,
  midnight still starts the next day, work runs again after a reset, and `aft budget` reports
  before and after.
- **F41 Who a line names, and what a conflict is** (ruling 2026-10-02). DESIGN 8, 9. The
  digest prompt has everything it writes, statements, summary and handoff, call the user
  "the user" and the agent "the agent", never by name or with a guessed pronoun; the first
  real memory had lines saying "himself". The
  decide prompt defines a conflict as two claims about the same thing that cannot both be
  true, and has the model check a named line is about the same thing before choosing
  supersede or conflict; the first real dream paired unrelated lines as conflicts. M2.6
  and M4.3 hold the prompts' text. Tests: the prompts' words. Review checkpoint: yes
  (prompts).
- **F42 Notes the user settles** (ruling 2026-10-02). DESIGN 6, 10. A conflict or question
  note waits on the user, and nothing told them one existed or let an agent settle it.
  `aft notes [path] [--all] [--json]` lists the open notes with their lines and quotes;
  `aft settle <page>:<line> --keep|--withdraw|--replace "<statement>"` settles one, moves
  the note verbatim to the archive, regenerates `needs-you.md` and commits. A replacement
  cites the note's verified, non-worker sources only, and a refused settle restores the
  archive. The memory block opens with "Needs your decision"
  when notes wait, telling the agent to raise them first and ask whether to settle now or
  skip; the session-start hook prints JSON carrying a notice the interface shows the user
  (Claude Code `systemMessage`, pi `ctx.ui.notify`). The aft skill holds the settle
  procedure: explain, recommend, ask, run `aft settle`. Tests: listing, counting, each
  choice, a replacement refused without verified quotes and leaving no trace, the commands,
  the block's part and its cap, the hook's JSON for both agents and plain text with no notes,
  and the pi extension's notice. Review checkpoint: yes (injection, trust).
- **F43 Redaction by shape only** (ruling 2026-10-03; FINDINGS T15). DESIGN 8, 12. The
  name=value rule redacted any value whose name held password, secret, token, key or
  credential, so token counts (`max_tokens`, `daily_token_budget`) and test passwords were
  lost. Delete that rule and the mysql `-p` rule; the built-in patterns keep only shapes
  that are credentials on their own (private key blocks, the prefixed token families,
  `Authorization:` headers, bearer tokens, URL credentials). New setting
  `capture.builtin_redaction` (bool, default true): false applies only
  `capture.redact_patterns`, everywhere redaction runs (filter, worker digest, Claude
  memory import). `aft forget pattern` is unchanged. Tests: `password = hunter2`,
  `"max_tokens": 4000` and `daily_token_budget = 200_000` survive; every prefixed token,
  key block, header and URL credential is still redacted; with the setting false a
  `ghp_` token survives and a configured pattern still redacts; the setting loads,
  defaults to true and refuses a non-boolean. Existing tests that relied on the deleted
  rules change to assert the value survives. Review checkpoint: no (lead reads it).
- **F44 Agents know the off-the-record markers** (ruling 2026-10-03). DESIGN 6, 12. An
  agent given a prompt starting with `aft off` read it as a request for a command aft does
  not have; nothing told it the markers exist. The block closes with one more line after
  the search line: `A prompt starting with "aft off", "aft on", "off the record" or "on
  the record" sets what afterthought keeps; it is not a request. Acknowledge it in a few
  words and do what the rest of the prompt asks.` It is part of the closing floor, kept
  whatever the budget. The `aft` skill says the same, near its command map. The filter is
  unchanged. Tests: the block ends with the line, under every budget the closing line
  survives; the echo filter still drops a repeated marker line; the skill text holds it.
  Review checkpoint: no (lead reads it).
- **F45 Markers given as a slash command** (ruling 2026-10-03). DESIGN 12. Typed as
  `/aft off`, a marker reaches the transcript as the agent's skill-call markup (Claude
  Code: `<command-message>aft</command-message>` `<command-name>/aft</command-name>`
  `<command-args>off</command-args>`; pi: its `<skill name="aft" ...>` expansion and the
  arguments after it), so no span opened and the session was archived whole. The Claude Code
  reader renders a slash-command prompt as the text typed, `/<name> <args>`; the pi reader
  does the same for a skill call, without the skill body. The markers also match with one
  leading `/` (`/aft off`, `/aft on`); `/off the record` is not a form anyone types and
  need not match. Matching stays at the start of a prompt only. Tests: the exact Claude Code
  markup for off and for on opens and closes a span; the pi form does the same; `/aft
  offline` and a slash command with other arguments do not; an archived slash-command
  prompt reads as `/<name> <args>`. Review checkpoint: no (lead reads the filter).
- **F46 What memory keeps** (ruling 2026-10-03). DESIGN 6 (short-term and long-term
  memory), 8 (the digest), 9 (a decision). Memory filled with lines a session could learn
  from the repository or that only described where the work stood. The digest prompt gives
  every fact one test: a future session would make a mistake, or ask the user again,
  without it. It names what is never a fact, long or short term: the status of a PR,
  task, branch or commit; who is working on what; a test's name or result; a review's
  outcome; what a change or fix did to the code; and anything the repository's own
  documents already say. Where the work stood belongs in the handoff, which only the next
  session sees as Last session. A short-term fact is a temporary condition that changes
  how an agent should act (something paused, a temporary setting, a freeze), not progress
  on the work. The decide prompt's skip applies the same test and list to the fact
  before it reaches memory. No rule, schema or script changes. Tests: the prompt and
  seed sync tests; the prompt texts hold the test, the list and the short-term definition.
  Review checkpoint: yes (both prompts). After merge, redigest two sessions and compare
  their facts with the pages they replace.
- **F47 Short-term memory follows the newest session** (ruling 2026-10-03). DESIGN 6
  (short-term memory), 9 (a run, R10, the report), 13. A recent line that a later session
  showed was finished or wrong stayed until it rolled off by session count, and a project
  with no new sessions kept its short-term memory indefinitely. (1) Reconcile: when a run
  applies a project's digests and `recent.md` has active lines, one call per part of the
  newest primary session applied (its archived entries, split under the digest's chunk
  cap) receives the numbered recent lines and returns, for each line that part shows is
  done, false or replaced, the line number, a quote and the entry it comes from. A new
  prompt `prompts/reconcile.md` and schema. The script verifies each quote against its
  entry as R1 does and withdraws the line on `recent.md` with that source; an unverified
  answer changes nothing and is reported. It never touches a long-term line, a line with an
  open note, or anything from a worker session, and it counts against the daily budget and
  is skipped when the budget is spent. (2) Backstop: `dream.short_term_days` (integer,
  default 7; below 1 is a config error) rolls off, in maintenance and exactly as session
  roll-off does, a recent line whose newest source is older than that many days. The
  report's Short term section lists withdrawn and expired lines. Tests: a done line is
  withdrawn with its quote; a wrong quote, a long-term line number and a worker session
  change nothing; the budget skip; the day backstop and its config; a run with no recent
  lines makes no call. Review checkpoint: yes (the prompt, `rules.py`, `apply.py`).
- **F48 `aft review`: clearing what memory should not keep** (ruling 2026-10-03; after
  F46). DESIGN 6, 9, 12. Memory written before F46 holds lines F46 would not keep.
  `aft review <project|global>` puts every active line on that project's fact pages
  (`recent.md` included; never digests, reports, archive or references), about twenty
  lines per call, to the model with F46's test and list (a new `prompts/review.md` and
  schema), and prints the lines it would withdraw, numbered, each with a one-line reason;
  it saves them to `reports/review-<slug>.md` and changes no page. `aft review <project>
  --withdraw <n,n,...|all>` withdraws the chosen lines from that saved list: struck through
  and dated like `aft settle --withdraw`, with the reason recorded, one commit. A line
  with an open note, or one that changed since the list was saved, is skipped and named.
  Seeded lines are reviewed like any other. The aft skill lists the command as one needing
  the user's go-ahead, with the list shown first. Tests: the list is saved and no page
  changes; withdraw by number and all; a changed line and a noted line are skipped; a
  number not on the list is refused; the budget is counted. Review checkpoint: yes (the
  prompt, withdrawal).
- **F49 Any directory can be a project** (ruling 2026-10-03; FINDINGS T28). DESIGN 7.
  Sessions outside a repository were unfiled and lost their project facts, work before
  `git init` among them, and two repositories with one directory name shared a project.
  `keys.resolve` step 4 resolves any directory to an unregistered project as DESIGN 7 says
  (repository main checkout, directory, `home` for the home directory itself); the system
  temporary directory, `~/Downloads` and `capture.exclude_paths` resolve to none. The
  resolution carries the path, the session-start record keeps it, and the dream registers a
  new project with that path; the dream finds an existing project by path, never by slug.
  A slug taken by another path gets parent names prefixed until unique; the digest
  directory uses the same slug. `aft project archive <slug>` / `unarchive <slug>`: an
  archived project (`archived: <date>` in `registry.yaml`) gets no memory block in its
  directory (only global), its pages and digests answer only `aft search --archive`, its
  sessions are still captured and dreamed; the skill lists both as needing the user's
  go-ahead. The dream report lists projects with no session in 90 days and projects whose
  recorded paths are all gone; nothing is archived automatically. Backfill: `aft redigest
  --unfiled` redigests every unfiled session still in the evidence archive, so each files
  under the project its recorded directory now resolves to; `--dry-run` lists them with an
  estimate of tokens. Tests: a plain directory, `home`, the temp directory, Downloads and an
  excluded path; two `foo` directories become `foo` and `<parent>-foo`; a project created
  before `git init` keeps its memory after; the dream registers the path and finds by
  path; archive and unarchive with the block and search; the idle and gone report lines;
  the backfill and its dry run. Review checkpoint: yes (`apply.py` registration).
- **F50 Search reaches every project** (ruling 2026-10-03; after F49). DESIGN 10
  (Search). Default `aft search` searches every project that is not archived: the
  current directory's project ranks first, then global pages, then other projects (a
  fixed rank tier ahead of BM25 within each tier), and each result names its project.
  `--project P` narrows to P and global; `--all` is kept as an accepted no-op alias for the
  new default. Unfiled digests and evidence are searched as another project. Injection is
  unchanged. Tests: a fact in another project answers from this one, below an equal match
  in this one; `--project`; an archived project answers only with `--archive`; the
  known-answer suite still passes; search time on the 20,000-row bench stays within 10% of
  main. Review checkpoint: no.
- **F51 One word settles the notes** (ruling 2026-10-03). DESIGN 10. The block asked "settle
  now or skip for now?" with no reply named, and "yes" to that question is ambiguous. The
  block's notes line, the hook's notice and the aft skill name one reply: "settle" goes
  through the notes now; any other reply, including carrying on, skips them. Also: the
  recent layer's floor no longer shares in the room the long-term layers divide (DESIGN 10:
  short-term memory takes only what they leave). Tests: the notes line and the notice name
  the reply; the short-term crowd-out test at 1,200 characters. Review checkpoint: no.
- **F52 Memory says only what was said** (ruling 2026-10-04; FINDINGS T32). DESIGN 6, 8,
  9, R2. Four wrong lines reached the memory block: a question of the user's recorded as a
  `[stated]` decision ("approved sending smaller subsections", quoting "Should we send
  smaller subsections...?"), a rule the user replaced (F51) left standing beside its
  replacement, a line made false by a later change (`aft settle` exists), and a statement
  naming a word its quote does not hold ("zebrafish" for "zebrafjord"). The turns digest
  (F55) already refuses, for its own items, a quote holding a question mark and a statement
  naming a name or number neither the user's turn nor a cited proposal holds
  (`digest.ungrounded`, DESIGN R2). F52 makes those checks the same everywhere a `[stated]`
  fact is made: (1) the question check moves into `digest.verify`, which both pipelines
  share; (2) the base pipeline grounds a `[stated]` statement's names and numbers in the
  entry it cites, with the same `digest.ungrounded`; (3) the dream's decide step refuses a
  new `[stated]` fact whose statement fails either check, and reports it. The checks read
  the same text in both steps: the question check reads only the user's own quotes, never an
  approved proposal; grounding reads the full text of the user's cited turn plus any cited
  proposal. A short-term line the decide step refuses on being lifted stays in `recent.md`. Prompts: the base digest and decide prompts say a question, a proposal or a
  request from the user is never a decision or a preference; the decide prompt says a new
  fact that changes how something is done, which an existing line describes, supersedes
  that line rather than adding beside it. Tests: the question, the replaced rule and the
  made-false line as fixtures, each refused or superseded; a statement naming a name or
  number its quote does not hold is refused; a stated fact with no question mark and
  grounded names still passes. Known limit: grounding checks names and numbers only (R2), so
  a plain word misread ("zebrafish" for "zebrafjord") is not caught.
  Review checkpoint: yes (prompts, rules).
- **F53 A directory excluded alone** (ruling 2026-10-04). DESIGN 7, 12. `~/src` holds every
  repository, so `capture.exclude_paths` (which reaches everything below a path) cannot
  exclude it, and the project `src` the backfill registered at it claims every plain
  directory below. New setting `capture.exclude_dirs`: directories excluded by themselves,
  nothing below them: a session run in one is not captured, it is never a project, it is
  refused before any lookup like the temporary directory, and a registered path equal to
  it claims nothing. Ted's config gets `exclude_dirs = ["~/src"]`, and the `src` project is
  archived (its five digests stay). Tests: a session in the directory is not captured, one
  in a plain subdirectory and one in a repository below are, and neither resolves to the
  excluded directory's project. Review checkpoint: no.
- **F54 Measuring what the digest gets right** (ruling 2026-10-04; FINDINGS T32). DESIGN 8,
  15. Digest quality is judged today by someone happening to notice a wrong line. A fixed test
  set decides every later change to what the model is given and asked. Test set: 15 to 20 of
  the user's real archived sessions (the user approved sending them to external models) and a
  few made-up transcripts for rare cases (a question that sounds like a decision, a rule later
  reversed, a fact made false later in the session, a tentative remark), stored under
  `eval/digest/` with each session's filtered entries. Answer key: the same extraction run
  three ways, by the local model, by Gemini and by a separate `claude -p` session; where they
  disagree, the lead settles it and the user settles what the lead cannot; the key lists each
  session's expected facts with their quote, tag, kind and term. `scripts/eval_digest.py
  [--variant V] [--model M]` runs a pipeline variant over the set and scores it: wrong facts
  (a fact the key does not hold, or holds differently), missed facts, wrong tags, questions
  recorded as decisions, and tokens spent; it writes a dated report. The current pipeline is
  the baseline. Reports break every score down by the model that wrote the answer. No pipeline
  change in this task. Tests: the scorer on hand-made answers.
- **F55 What the digest reads** (ruling 2026-10-04; after F54; FINDINGS T33). DESIGN 8. The
  user's words are 5 to 13% of what a digest call reads; tool output is 34 to 89%, mostly
  source lines kept for holding a word like "error" (T33). On the F54 per-turn key
  (`<data>/key-turns.json`: 266 user turns, 79 durable items) today's digest finds 23% of the
  durable items with the local model; one narrow question per user turn finds 80% at 82%
  precision with the same model. Build, as `digest.pipeline = "turns"` (the default once the
  scores below decided it; "base" stays selectable):
  (1) Input: a tool call becomes one compact action line naming the tool and its target (`Edit
  src/aft/digest.py`, `Bash: uv run pytest`), with no arguments or output; error lines are kept
  only from commands that failed; code blocks, diffs and tables in assistant text collapse to a
  placeholder naming them. The archive holds a tool's output, not its call, so a successful
  command shows nothing until capture keeps the call's name and target.
  (2) Per user turn, with the end of the assistant message before it and the action lines
  between: one call over a batch of turns asking, for each, which decisions, preferences,
  rules or facts about the user's setup the user settled, each with a statement and the
  user's exact words; a question, a proposal, a request for the task at hand and pasted
  material are never one of those. Every turn gets at least one item, an outlet label when it
  settles nothing, and a turn the answer skips is asked again once on its own: asking for the
  fewest items left turns unanswered (measured 2026-10-04). Batches hold at most 15 turns and
  DESIGN 13's input cap. A `[stated]` statement's names and numbers are grounded in the
  user's turn, or in one cited sentence of the message it answers (DESIGN R2, ruling
  2026-10-05); a quote holding a question mark, or inside a paste or a command block, is
  refused (F52, DESIGN 6). Measured on the local model: base 18 of 69 durable turns at 60%
  of `[stated]` on a durable turn, turns 34 at 78%; the default is "turns".
  (3) Observed facts: one call over assistant prose and action lines for what the session
  observed about the system that the repository does not record.
  (4) Final state: one call over the session's items in order: a later item on the same
  subject replaces an earlier one only when the user stated the change explicitly ("change of
  plan", "instead", "no longer"); speculation never replaces anything; the replaced item is
  dropped and named in the digest's report.
  (5) Summary and handoff in their own call. A refused summary does not drop its part: the
  part's other calls still run, the refusal is listed with the dropped items, and a job is
  refused only when it gives neither a summary nor a fact. An approval ("yes, do option B") is a decision
  item carrying the exact words of the proposal it approves from BEFORE; the script verifies
  the user's words in the turn and the proposal's words in the assistant entry the turn
  answers, grounds the statement in both, and writes the line with both as sources
  (DESIGN R2, ruling 2026-10-04). The script sets the source tag from whose words
  the quote is (`[stated]` only from a user turn) and verifies every quote as today.
  Scoring: `scripts/eval_digest.py score-turns` scores a run against key-turns.json (durable
  items found, wrong items, per writing model); the PR carries base and turns scores for the
  local model. The default moves to "turns" only if it finds more and is wrong less often.
  Known limit, for the scores to show: the turns calls have no `[suggested]` or `[inferred]`
  outlet, and the observed call reads no user entry, so a log the user pastes gives no
  `[observed]` fact as the base pipeline's does.
  Review checkpoint: yes (prompts, schema, filter).
- **F56 The digest checks its own answers** (ruling 2026-10-04; after F55). DESIGN 8. Each
  proposed item gets one short call with its quote and the two entries before and after it,
  asking yes or no: did the user assert this, or ask, propose or paste it; and is it still in
  effect by the end of the session. Kept only on yes to both; a refusal is reported. A verifier
  from the same family as the writer shares its blind spots, so F56 measures on the F54 key a
  same-model verifier against a small model of a different family (run locally, short inputs
  only) and keeps whichever scores better; a different family, not a stronger model. The
  verifier's endpoint and model are their own settings, defaulting to the digest's model,
  so a second model is a config change. Build and measure the same-model verifier first,
  behind `digest.verify_items` (default false until it scores better); the cross-family
  measurement waits until a second local model is served. The PR carries the scores and
  the extra tokens. Review checkpoint: yes (prompt, schema).
- **F57 What the working agent points at** (ruling 2026-10-04; an experiment, after F55).
  DESIGN 8. The working agent sees each decision as it happens. Measured on F54 against F55's
  structural flags alone: (a) a `Memory:` footer the agent writes when a response settles a
  decision, a preference or a fact the repository does not record, each line quoting the
  user's own words where it names a decision; (b) a bare look-here marker. The digest treats
  either as a pointer only: a `[stated]` fact still quotes the user's own turn, verified by
  the script, and the pipeline must score no worse when the agent writes none. No skip
  marker: text the agent marks skippable is never removed. The user's global agent
  instructions change only if the user adopts the winning form. Review checkpoint: yes.
- **F58 Same-day withdrawal of a recent line** (ruling 2026-10-05). DESIGN 6, R4. When a
  dreamed session withdraws a short-term line (F47), its evidence is newer when its entry time
  is later than the line's newest source, not only when its day is: "X is paused" at 09:00 is
  withdrawn by "X is resumed" at 15:00 the same day. Supersede of long-term lines still
  compares days. Tests: same-day later entry withdraws; same-day earlier entry does not; an
  entry with no time falls back to the day. Review checkpoint: no.
- **F59 Statements that read on their own** (ruling 2026-10-05; from a review of Honcho's
  extraction prompt). DESIGN 8, R2. (1) Dates: each turn block shows the turn's date and
  weekday, and the turns prompt says a relative date the user gives ("tomorrow", "next
  Friday", "in two weeks") is written as the absolute date it means, counted from the turn's
  date. Grounding accepts a date in the statement when the script computes it from a relative
  date in the turn and the turn's time (today, tomorrow, yesterday, a weekday name with or
  without "next" or "this", "in N days" or "in N weeks"); any other date stays a name or
  number the turn must hold. (2) Pronouns: the prompt says a statement leaves no "it",
  "this", "that", "there" or other pronoun pointing outside itself, naming what it means;
  a name taken from the answered message is cited as R2 already allows. Tests: "pause it
  until tomorrow" on a Monday turn becomes Tuesday's date and is kept; a date the turn does
  not imply is refused; each relative form above computes its date; the turn block shows the
  date. Scored on the F54 key with the local model at temperature 0 against the turns
  pipeline before it; merged only if it scores no worse. Review checkpoint: yes (prompt).
- **F11 Shared sqlite plumbing** (low priority; after M7). One module holds WAL mode, the
  busy timeout and the write lock that `index.py`, `evidence.py` and `queue.py` each
  repeat; the three import it. Behavior does not change. Tests: the existing suites pass,
  and one test per store checks WAL mode and lock serialization through the shared module.
- **F12 Library trust** and **F13 Library import safety** (after M7; M7 milestone review).
  The findings and their fixes are listed in the review and on the build board: a library
  quote is never a primary-session signal for R3; `propose` takes authority and roles from
  the import record; project names are registered slugs; map descriptions are checked;
  quotes are checked against extracted text and the recorded hash; symlinks are refused;
  tooling is `exclude`; re-import removes what is excluded; slugs never take `collection` or
  `index`; frontmatter is written with a YAML dumper and linted before the commit.
- **F14 Verified descriptions** (after F12). DESIGN 11 step 4. `aft library describe
  <manifest> <descriptions-file>`: each description names a document of an imported
  collection and carries `what`, `version` and `when_to_use` claims, each with a quote. The
  command checks every quote against the document's extracted text and its recorded hash,
  writes the verified claims into the document's reference page under a `## Description`
  section with their sources, lists failures in the import report, and commits. The skill
  and the survey's procedure name this command and drop the instruction to edit pages by
  hand. Tests: a verified description lands on the page with its quote; a claim whose quote
  is not in the document is refused and reported; a description for a file outside the
  collection is refused; a rerun with the same file changes nothing.
- **F5 Collection authority** (after M7.6). DESIGN 11, Authority. The manifest gains
  `authority: authoritative | reference` (default `reference`), validated by M7.2's
  validator. The import skill asks the user for it and records the answer; the agent never
  chooses it. `aft library propose` tags facts from an authoritative collection's
  `original`, `derived` and `errata` files `[observed]`, and every other imported fact
  `[inferred]` or `[suggested]`; `[stated]` is always refused. The import report and
  commit message name the authority. Tests: an authoritative collection's document fact
  enters as `[observed]` and, with no conflict, goes active in one dream run; the same fact
  from a reference collection stays a candidate; a notes-file fact in an authoritative
  collection stays a candidate; an authoritative fact contradicting a stated `decision`
  becomes `needs you`; a manifest without `authority` imports as `reference`.
- **F6 Shipped text names no one** (any time). Prompts, JSON schemas, skills, CLI output and
  code comments say "the user" (or "you" in user-facing text), never a person's name, and
  use no example drawn from one user's projects. Covers `src/aft/prompts/digest.md` and
  `digest.schema.json` (a prompt change, so it gets its review checkpoint), and comments in
  `inject.py`, `digest.py`, `filter.py` and `redact.py`.

## 6. Ted's checklists at milestone ends

### 6.1 M1

1. Run `aft init`, `aft project add ~/src/afterthought`, and the M1.5 seed script against
   the real vault.
2. Run `aft install claude --dry-run`, check the diff, then install. Same for pi.
3. Start a new session in each agent in `~/src/afterthought` and check both show the same
   memory block.

### 6.2 M2

1. If the model endpoint needs a key, put it in `~/.afterthought/env` as
   `<the variable named in model.api_key_env>=<key>`, with `chmod 600`.
2. `aft install timer`. Set `capture.worker_paths` for firstmate's worktrees
   (`~/.treehouse`) and the validation gate (`~/.no-mistakes/worktrees`).
3. Work normally for a day, then read the digests in `~/afterthought/digests/` and check
   their facts and quotes.
4. Try `aft off` and `aft on` in a session and confirm nothing between them appears.

### 6.3 M4

1. Read the first week of dream reports and `needs-you.md`.
2. Revert one dream commit with `git revert` to confirm it works.

### 6.4 M7

1. Import the RV-C documentation as `authoritative` and the Victron documentation as
   `reference`, through the import skill.
2. Check the import reports, then run known-answer searches: RV-C DGN definitions and their
   pages, and a Victron device setting.

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

Claude builds every remaining task, follow-up and milestone, one PR per task, in the order
above. Branches firstmate left in flight are picked up as they stand, with their open review
findings sorted under rule 10.
