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
You read one part of a working session between the user and an AI assistant, often a
coding agent, and you extract what a future session would need to know. You are building
the project's memory, so precision matters more than coverage. Return nothing rather than
something uncertain.

Each entry of the session begins with a label in brackets, such as [#3], then who wrote it
and when. Everything you write comes from these entries and names the entry it comes from
by its label.

Keep only what a future session could not learn by reading the code or the git history:
decisions and their reasons, preferences, how things are actually configured, lessons
learned the hard way, procedures, and where authoritative information lives. Keep what
was surprising or decided. Skip routine steps, file listings, code, and anything already
recorded in git.

A fact includes what the user states about themselves: interests, hobbies and skills they
are learning, activities and events they took part in, things they own or bought, plans,
people in their life, places, and quantities and dates ("I've restored three vintage
radios since I started the hobby"). A preference includes personal tastes ("I prefer
matinee showings"). A fact about the user's own life is global scope.

Every fact passes one test: a future session would make a mistake, ask the user again,
or give less fitting help without it. Never a fact, long or short term: the status of a
pull request, task, branch or commit; who is working on what; a test's name or result; a
review's outcome; what a change or fix did to the code; and anything the repository's own
documents already say.
Also never a fact: a one-time order to do a piece of work, given or approved by the user,
whether or not it was carried out. A standing rule, a permission, and the choice an
approval settles are not such orders.

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
- scope: global for the user's preferences and environment that apply to every project,
  and for a fact about the user's own life; otherwise project.

Also write:
- summary: one to four sentences on what happened.
- handoff:
  - goal: one sentence stating the user's primary goal or request for this session, with
    its entry label.
  - in_flight: what was being worked on when this part ended, with its entry label.
  - failed: approaches that failed, with entry labels.
  - open: questions and next steps left open, with entry labels.
  - files: list of relative file paths touched or relevant to the in-flight work.
  Use empty values when there is nothing to hand off.

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

A fact earns its place in memory only when a future session would make a mistake, ask
the user again, or give less fitting help without it. A fact about the user's life,
interests, tastes or plans passes it. A dated event is a durable record ("On 2024-03-02
the user adopted a dog"), not a fact that stops being true, so it is kept long-term
with its date in the statement. Never a fact, long or short term: the status of a pull
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

Each line shows its source tag in brackets, its date, and one quote from its evidence; the
new fact shows its own tag, date and quote. A tag says who asserted a line: [stated] the
user, [observed] a session, [inferred] or [suggested] the model, which is tentative. Newer
evidence about the same thing that says something different means the thing has changed:
supersede. Evidence that is not newer does not replace a line: confirm when the two say the
same thing, conflict when they say different things that cannot both be true.

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
- **F60 Current truth stays current** (ruling 2026-10-05; the idea from the longmem.dev
  benchmark). DESIGN 6, 9, R4. An end-to-end eval of what the memory block says after a
  sequence of sessions, where F54 scores the digest alone. Scenarios in `eval/current/`, each
  a short scripted sequence of sessions in afterthought's own setting, written as transcripts
  in the repo: a preference the user reverses; "X is paused" then "X is resumed" the same day
  (F58); a temporary setting that expires; a rule the user replaces (F51's case); a fact
  restated unchanged. `scripts/eval_digest.py current` runs each sequence through capture,
  digest, dream and inject in a fresh temporary AFT_HOME and vault per scenario, refusing the
  real home and vault as the F54 harness does; no real session and no hook is involved, so
  nothing reaches the real archive, vault or memory block. Scoring is mechanical, no model
  judge: per scenario, the block states the current value, and holds no superseded one
  (contradiction rate). The report gives both per scenario and in total, per model. Review
  checkpoint: no.
- **F61 Search finds other forms of a word** (ruling 2026-10-05). DESIGN 14 (search). The
  index's collections use FTS5's built-in `porter unicode61` tokenizer, so "pause" finds
  "paused" and "configure" finds "configured"; whole-word matching found neither. The build
  stamp moves to `rows-5`, so an index from before is refused with the `aft reindex` repair.
  Tests: other forms of a word find the line. Review checkpoint: no.
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
- **F62 Obsidian graph view configuration** (ruling 2026-10-06). DESIGN 3 (vault layout).
  Generate an opinionated `.obsidian/graph.json` during `aft init` so opening the vault in
  Obsidian provides a color-coded graph view matching afterthought's fact kinds and folder
  hierarchy without manual UI setup. `src/aft/vault.py` defines `OBSIDIAN_GRAPH`, adds it to
  `initial_files()`, and un-ignores `!.obsidian/graph.json` in `gitignore_text()`. Tests:
  `tests/test_vault.py` verifies `.obsidian/graph.json` is created, valid JSON, contains expected
  color groups, and is tracked by git. Review checkpoint: no.
- **F63 Digest handoff captures session goal and files** (ruling 2026-10-06). DESIGN 8 (handoff)
  and 10 (injection). Enhance session handoff in digests so each session captures (1) the user's
  primary goal/intent (`goal`: text and entry label) and (2) relevant files touched or left
  in-flight (`files`: list of relative paths). Update `src/aft/prompts/digest.schema.json`,
  `src/aft/prompts/digest.md`, `src/aft/digest.py` (`grounded_handoff`, validation, serialization),
  and `src/aft/inject.py` (`handoff()` rendering under `Last session` with `Goal:` and `Files:`).
  Tests in `tests/test_digest.py` and `tests/test_inject.py`. Review checkpoint: yes (prompt and schema change).
- **F64 Invisible character and prompt-injection sanitization in filter** (ruling 2026-10-06).
  DESIGN 5 (filtering and privacy). Harden `src/aft/filter.py` against indirect prompt injection
  and memory poisoning. Strip invisible Unicode characters (zero-width spaces, word joiners,
  byte-order marks, bidirectional overrides) from all incoming transcript entries. Neutralize
  high-confidence adversarial override patterns in tool outputs and untrusted assistant echoes
  before entries are archived or digested. Tests in `tests/test_filter.py`. Review checkpoint: no.
- **F65 OKF v0.2 interlinked wiki compounding** (ruling 2026-10-06). DESIGN 11 (library and
  references). Enable automatic bidirectional wikilink (`[[target]]`, `[[target|alias]]`)
  resolution and backlink tracking between projects, library references (`references/`), and
  global preferences. `src/aft/page.py` extracts wikilinks into `Fact` and `Page` objects;
  `src/aft/index.py` stores links in a `page_links` SQLite table and maintains incoming/outgoing
  edges across reindexes; `src/aft/search.py` adds backlink queries (`aft search --links <target>`)
  and displays `## Backlinks / Referenced In` on `aft read references/<name>`. Prompt and
  synthesis instructions encourage linking canonical concepts without inventing references.
  Tests in `tests/test_page.py` and `tests/test_index.py`. Review checkpoint: no.
- **F66 Persisted cooldown on model errors** (ruling 2026-10-06). DESIGN 9 (model calls) and
  13 (background work). Protect against repeated blocking timeouts and log flooding when the
  model endpoint is unreachable or returns 5xx/429 errors. `src/aft/model.py` persists cooldown
  state to `~/.afterthought/model-cooldown.json` (15-minute default) on network/server errors;
  `complete_json()` and `work.run()` fail-fast while active without network calls; `aft model cooldown`
  and `--clear` inspect and reset status; `aft budget reset` also clears cooldown. Tests in
  `tests/test_model.py` and `tests/test_work.py`. Review checkpoint: no.
- **F67 Deterministic temporal query parsing in aft search** (ruling 2026-10-06). DESIGN 10
  (search). Parse natural temporal language (today, yesterday, last 3 days, past week, 2026-09,
  since Tuesday) in search queries deterministically without an LLM. Pure-Python regex parser in
  `src/aft/temporal.py` extracts ISO start/end boundaries, strips temporal expressions from the
  FTS5 lexical query string, and applies date range bounds to the SQLite query using the `time`
  column. Tests in `tests/test_temporal.py` and `tests/test_search.py`. Review checkpoint: no.
- **F68 Code-span masking for clean wikilink extraction** (ruling 2026-10-06). DESIGN 11 (library
  and references). Ensure wikilink extraction (`[[target]]`, `[[target|alias]]`) in `src/aft/page.py`
  masks fenced code blocks (``` or ~~~) and inline backtick code spans (`...`), preventing
  false-positive graph edges from example snippets, shell commands, or diffs. Tests in
  `tests/test_page.py`. Review checkpoint: no.
- **F69 LongMemEval-S evaluation benchmark harness** (ruling 2026-10-06). Add reproducible
  benchmarking runner in `eval/longmemeval.py` measuring retrieval recall (Recall@1, Recall@3,
  Recall@5, Recall@10) and ranking quality (Session NDCG@10) across multi-session test sets in
  isolated sandbox vaults. Metric calculation in `src/aft/eval.py` and CLI support in
  `src/aft/cli.py` (`aft eval benchmark`). Tests in `tests/test_eval.py`. Review checkpoint: no.

- **F71 LongMemEval-S pipeline mode** (ruling 2026-10-08). Add `--mode pipeline` to
  `eval/longmemeval.py`: each question's haystack goes through the real capture, digest and
  dream path on the configured model, instead of the raw dumps the baseline stores.
  - **Sandbox config:** the sandbox `config.toml` copies the `[model]` table (endpoint, name,
    extra_body) from the caller's config, sets `work.daily_token_budget` and
    `evidence.retention_days` high enough never to bind, and sets the digest and dream
    `quiet_minutes` to 0.
  - **Project:** a git repository registered as the sandbox's one project, created outside
    the system temporary directory (under `--workdir`), because a directory under it
    resolves to no project. The run asserts the project resolves to a slug.
  - **Replay:** each session is written as a Claude transcript and enqueued, in haystack date
    order, and `work.run` then the dream schedule run once per session date with `now` just
    after that date, so supersede and short-term expiry see the dataset's order.
  - **Scoring:** three session rankings per question, each scored with the existing metrics:
    (a) `digests`, digest rows only, attributed by digest file name; (b) `coverage`, the
    fraction of answer sessions cited by any page line's source (an upper bound for curated
    memory, not a ranking); (c) `full`, production search over everything, with a page hit
    credited to the sessions its matched line cites, never to every session a section
    cites.
  - **Durability:** one JSONL record per question written as it finishes, with sessions
    digested, empty, below the floor and failed, `work.run`'s stopped reason (a stopped
    question is flagged, not scored), tokens from the sandbox `usage.jsonl`, wall time,
    model name, pipeline and prompt hashes. `--resume` skips ids already recorded.
    `--keep-sandbox` keeps every sandbox; a failed or stopped question's sandbox is always
    kept.
  - **Sample:** `--ids FILE` runs a fixed list of question ids. `--jobs N` (at most 3)
    runs questions as separate processes, since the sandbox sets process environment.
  - **Tests** in `tests/test_eval.py` with the fake endpoint: a two-session haystack runs
    end to end, the config overrides land, attribution credits only cited sessions.
    Review checkpoint: yes.

- **F72 LongMemEval-S pipeline diagnostics** (ruling 2026-10-08). In `eval/longmemeval.py`
  pipeline mode:
  - **Line matching:** a page hit credits the line the query ranks first among the hit
    section's injected lines by stemmed BM25, in an in-memory FTS5 table built with
    `index.TOKENIZER` the way `aft.dream.decide.related` builds its own; no line matching
    any query word credits no session. Replaces raw word overlap.
  - **Records:** each question's record lists every session's outcome by id (digested,
    empty, below the floor, failed, left queued) and, for the `full` ranking, each ranked
    session with the hit that credited it (collection, path, heading, matched statement).
  - **Sandboxes:** kept by default; `--discard-sandbox` deletes a finished, scored one.
  - **Rescore:** `--rescore` re-scores the sandboxes a JSONL names without calling the
    model, writing a new JSONL, so a scoring change can be measured on kept runs.
  - Tests in `tests/test_eval.py`. Review checkpoint: no.

- **F73 Broader memory: the user's life, not only the work** (ruling 2026-10-08, DESIGN 2
  and 6 Kinds). Prompt changes only; the trust rules, schemas and grounding are unchanged.
  - **`turns.md`, `digest.md`, `summary.md`, `reconcile.md`:** the session is between the
    user and an AI assistant, often a coding agent, not only a coding agent. A `fact` includes
    what the user states about themselves: interests, hobbies and skills they are learning,
    activities and events they took part in, things they own or bought, plans, people in their
    life, places, and quantities and dates ("I've restored three vintage radios since I started
    the hobby"). A `preference` includes personal tastes ("I prefer matinee showings"). A fact
    about the user's own life is `global` scope.
  - **`decide.md`, `review.md`:** the worth-keeping test becomes: a future session would
    make a mistake, ask the user again, or give less fitting help without it. A fact about
    the user's life, interests, tastes or plans passes it. A dated event is a durable
    record ("On 2024-03-02 the user adopted a dog"), not a fact that stops being true,
    so it is kept long-term with its date in the statement. The never-a-fact list
    (work status, test results, review outcomes, what a change did) stays.
  - **Measure:** before merging, lead reruns the six F72 diagnostic questions
    (`~/src/aft-eval-data/longmemeval/pipeline/diag6-ids.txt`) and compares coverage and
    digest recall with the F72 run; and runs `scripts/eval_digest.py` on the existing
    coding-session key to check the coding-memory precision does not drop.
  - Tests: the prompt-shape tests that pin these prompts' text are updated. Review
    checkpoint: yes (prompt change).
- **F74 LongMemEval-S searches from the sandbox project** (ruling 2026-10-08). Pipeline-mode
  scoring searches as a session in the sandbox project would (`project=` the sandbox slug,
  not `all_projects=True`), so the tier order matches production: project rows first, global
  pages next. With no project pinned, global pages outrank every project row whatever their
  relevance. Baseline mode is unchanged. Tests in `tests/test_eval.py`. Review checkpoint: no.

- **F75 Trust the digest-time quote check** (ruling 2026-10-09; roadmap M6; changes R1 and
  DESIGN 8). Today the dream re-verifies every quote against the evidence archive, which
  `aft run` purges after `evidence.retention_days`, and quotes from excluded instruction
  files and imported Claude memory are proven only by `~/.afterthought/library/verified_quotes.json`,
  outside the vault. So R3 signals past retention count zero, a digest waiting past
  retention loses every fact to R1, and a cloned vault rejects its waiting digests.
  - **Record at digest time:** when a source's quote verifies against the archive, the
    digest's frontmatter source records `verified: <UTC date>` and `check:` a hash of
    (session id, entry id, quote). Library-proposal and Claude-memory imports write the
    same record into their vault files, and `verified_quotes.json` stops being a source of
    truth (kept only as a cache, or removed).
  - **R1 at dream time:** a source whose record is present and whose hash matches its
    session, entry and quote passes R1 without reading the archive. A source with no
    record (a digest written before F75) or a mismatched hash (an edited quote) is
    verified against the archive as today, and rejected if that fails.
  - **Other dream-time archive reads:** list every place the dream reads the archive (R2's
    approval and name checks, R4's newer-evidence date, R10's done check, decide's quote
    rendering). Each either uses what the digest already recorded, or records what it
    needs at digest time; none may fail only because evidence expired. Put the list in
    the PR body.
  - **R3 aging** uses the source's recorded date, not archive presence.
  - **Purge:** unchanged; it no longer affects R1 for recorded sources.
  - **DESIGN:** rewrite R1 ("at least one source whose quote was verified against the
    archive when the digest was made, or is verified now") and the DESIGN 8 and 14 text on
    expired evidence to match, in the present tense.
  - **Tests:** purge then dream still applies a waiting digest; a vault with an empty
    archive (a clone) applies its waiting digests; an edited quote with a stale hash is
    rejected once its evidence is gone; a pre-F75 digest falls back to the archive; R3
    counts a signal older than retention at half weight. Review checkpoint: yes (trust
    rule; lead reviews in full).

- **F76 A basic user profile, coding memory first** (ruling 2026-10-09; roadmap 9.3; builds
  on F73). Memory keeps a short profile of who the user is, without lowering what coding
  memory keeps or admitting noise from coding sessions.
  - **Profile categories:** `turns.md` and `digest.md` keep F73's wording for what a
    fact about the user covers; the named category list lowered coding recall in
    measurement (2026-10-09), and the profile is filled by F73's definition of a fact
    about the user.
  - **Placement:** profile facts are `global` scope and land on a global profile page
    (`global/profile.md`, sections by kind as other pages), injected with the global
    preferences under the existing `inject.max_chars` budget, after the project's own
    sections so coding memory is never cut for profile lines.
  - **Test set:** add synthetic sessions under `eval/digest/synthetic/` written fresh (never
    copied from LongMemEval or another benchmark): mixed coding and personal sessions with
    expected profile facts and expected non-facts. Examples are neutral and invented.
  - **Measure, before merging:** profile recall on the synthetic set rises; coding precision
    and recall on the existing `eval_digest.py` key (`score`, `score-turns`) do not fall
    beyond run-to-run noise; LongMemEval-S diagnostic six and the 60-question sample are
    confirmation only, never tuned against.
  - Tests: prompt-shape tests updated; inject places the profile after project sections and
    is the first layer cut under budget. Review checkpoint: yes (prompt change).

- **F77 Observed by key tokens, not verbatim quotes** (ruling 2026-10-09; amends the M2
  gate in `digest.py`). An agent's statement quoted from its own message is `[observed]`
  when what it reports is backed by a tool result in the same part, judged by the tokens
  that can be checked, not by finding the agent's whole sentence verbatim in tool output.
  Agents paraphrase what a command showed, so the verbatim test downgraded nearly every
  report to `[inferred]` (coding key: 4 observed facts against 172 before M2), and
  `[inferred]` lines never pass R3 in practice (real vault: 552 candidates, none promoted).
  - **Key tokens:** the statement's checkable tokens: a word holding a digit or one of
    `_/.:-` or a capital past its first letter (`GitHub`, `SessionStart`), and a
    backticked span with no whitespace. A word whose only capital is its first letter
    (`Claude`, `Groq`) is a plain word here, and a backticked span holding whitespace (a
    command such as `aft project add`) is checked word by word under the same rule. Hosts,
    ports, paths, versions, flags and config keys hold a digit or one of `_/.:-`, so they
    are tokens. Plain words are not checked.
  - **Rule:** a quote from an assistant entry is `[observed]` when the same part holds at
    least one tool result or error, and every key token of the statement appears verbatim
    in that part's tool results or errors (case-sensitive; a backticked span counts as one
    token). A quote from a tool or error entry stays `[observed]` as today. A statement with
    no key token, or with any key token absent from the tool output, is `[inferred]` as
    today.
  - **Unchanged:** `observed.md`'s exclusion of plans, intentions, guesses and
    explanations; the decide prompt's never-a-fact list, which handles work-status noise.
  - **DESIGN:** the source-tag paragraph (section 6) says when an agent's report counts as
    seen in a tool result, in the present tense.
  - **Measure, before merging:** lead reruns `eval_digest.py run` on the coding key and
    compares `[observed]` counts and `score`/`score-turns` with the 2026-10-09 runs, and
    hand-checks 30 newly observed facts for guesses.
  - **Tests:** a paraphrased report whose tokens all appear in the tool output is
    observed; one with a token missing (a wrong port) is inferred; one with no key token is
    inferred; a quote from a tool entry stays observed; a part with no tool result yields
    no observed assistant fact. Review checkpoint: yes (authority rule; lead reviews in
    full).

- **F78 Check observed reports against the raw transcript** (ruling 2026-10-09; amends F77
  and DESIGN 8). The filter keeps only error and failure lines and `path:line` references
  from tool output, so a version, port or test count an agent reports from a command that
  succeeded never reaches the digest's tool entries, and F77's check can back only reports
  drawn from errors (coding key: 15 observed facts against 172 before M2).
  - **Check:** at digest time, the F77 key-token check for an assistant report runs against
    the unfiltered tool results of the same part's span, read from the transcript the
    digest already reads (the raw entries between the part's first and last entry), instead
    of the filtered tool entries. The rule is otherwise F77's, unchanged.
  - **Nothing new is stored or sent:** raw tool output never enters the archive, the
    vault, an index or a model call; it is held in memory for the check and dropped. The
    model's input is unchanged.
  - **Record:** a report that passes is written to the digest as `[observed]` as today.
    Every later stage (dream, reconcile, settle) takes the digest's tag and never reruns
    the tool-backing check against the archive; list each place the M2/F77 gate runs today
    and how it is handled in the PR body.
  - **Missing transcript:** when the raw entries cannot be read (a transcript moved or
    rotated), the check falls back to the filtered tool entries, as F77 does.
  - **DESIGN 8:** say that the filter decides what is kept, and that the digest's
    observed check reads the unfiltered tool output in memory without keeping it; DESIGN 6's
    F77 sentence says the check reads the session's tool output.
  - **Measure, before merging:** lead reruns `eval_digest.py run` on the coding key and
    compares observed counts and `score-turns` with F77's run (15 observed, 42 of 76
    found), and hand-checks 30 newly observed facts for guesses.
  - **Tests:** a report whose tokens appear only in successful (filtered-out) tool output
    is observed; the same report with the transcript unreadable is inferred; raw tool text
    appears in no archive row, digest or model request (assert on the fake endpoint's
    requests). Review checkpoint: yes (authority and privacy; lead reviews in full).

- **T37 One archive lookup for a source's role and time** (follow-up to F75, 2026-10-09).
  `src/aft/dream/apply.py` queries the evidence archive for an entry's role and time inline
  in three places. Add one function in `src/aft/evidence.py` that returns an entry's role
  and its UTC said-date (or None when the row is gone), use it in all three places, and
  delete the inline queries and their duplicated date parsing. Behavior is unchanged; the
  existing F75 tests must pass untouched. Review checkpoint: no.

- **F80 Coding-search eval: where the answer ranks** (ruling 2026-10-10; lands before F79).
  `aft eval` passes an entry when its answer is in the memory block or anywhere in the
  results, so a ranking change cannot move it. This adds a rank measure over search alone,
  and a frozen copy of the real vault to run it on.
  - **`aft eval --ranks`:** for each entry, runs search as a session in the entry's project
    would (every project that is not archived, the entry's project first; not narrowed with
    `--project`), with the injection block left out. A hit holds an expected item when the
    text the hit leads to holds it: a page or digest hit's section under its heading, an
    evidence hit's archived entry. Every collection counts the same way, so the measure does
    not favour curated rows. The entry's rank is the 1-based position of the first hit
    holding any expected item, or none within `--limit` (default 10 here). An `expect_not`
    item is reported with the rank it surfaced at.
  - **Output:** one line per entry (rank, the matching hit's collection and project), then
    recall@1/3/5/10 and MRR over the set, each also split by the matching hit's collection.
    `--json FILE` writes the per-entry records so two runs can be compared entry by entry.
    The existing pass/fail mode is unchanged. No model is called.
  - **`search()`:** gains a keyword naming the project that ranks first without narrowing
    the query, which is how a session's search behaves from its working directory.
  - **Snapshot:** `scripts/eval_search.py snapshot DIR` copies the vault (a clone at its
    current commit), `registry` and config, and the evidence archive
    (`~/.afterthought/evidence.sqlite`, which is outside the vault and purged after
    `evidence.retention_days`) into `DIR` as a home of its own, and rebuilds the index there
    with `aft reindex`. `scripts/eval_search.py run DIR KEY` runs `aft eval --ranks` with
    `AFT_HOME` set to `DIR`. It refuses to run against the real home, like
    `eval_digest.py`. The snapshot's vault commit and archive row count are recorded in
    `DIR/snapshot.json`.
  - **The key (lead, not in git):** about 40 entries in
    `~/src/aft-eval-data/search/coding.yaml`, over a snapshot in
    `~/src/aft-eval-data/search/snapshot-<date>/`. Questions are taken from what was
    actually asked in the exported coding sessions (`~/src/aft-eval-data/sessions/`), worded
    as an agent would search, never rephrased from the answer line. Each expected item is
    checked present in the snapshot. The set covers answers that live only on a page, only
    in a digest, and only in evidence, with at least 5 entries whose words also match
    another project's memory, so other projects ranking last stays tested.
  - **Baseline:** before F79's PR is reviewed, the lead runs the key on today's ranking and
    keeps the JSON beside the key.
  - **Tests** in `tests/test_eval.py` over a fixture vault with invented text: rank found
    in each collection, not found past the limit, the injection block not counted, another
    project's row reached but ranked after the entry's project, the snapshot refusing the
    real home. Review checkpoint: no.

- **F79 Rank current memory by BM25 across collections** (ruling 2026-10-10; amends H5's
  curated-first order and F74's project-before-global tier, DESIGN 10). On the 60-question
  LongMemEval-S sample, raw dumps reach recall_any@1 0.833 and production search over the
  pipeline's memory 0.600: within a tier every page and digest row outranks every evidence
  row, so a weak page match buries the evidence entry that holds the answer.
  - **Tiers:** two. The current project's rows and the global pages are tier 0; other
    projects' rows and unfiled text are tier 1. Tier 1 still ranks after all of tier 0.
    With `--project P` the query is narrowed as today, and its rows are all tier 0.
  - **Order inside a tier:** by BM25 score (`index.rank_expression`) across pages, digests
    and evidence (and library and archive when they are searched). Curated rows (pages,
    digests) win near-ties only: a curated row's score is multiplied by 1.10 before the
    sort (scores are negative, so this moves it up), which puts it ahead of a raw row whose
    score is within 10% of its own. The factor is a module constant, not a setting.
  - **One key:** the SQL `ORDER BY` in each collection's query and its per-session
    partition, and the Python merge across collections, sort on the same (tier, adjusted
    score) key, so no collection's `LIMIT` drops a row the merge would have kept. The
    per-session evidence collapse is unchanged.
  - **Scale:** each FTS5 table computes BM25 over its own rows, so scores from different
    collections are compared as they come, without normalizing. Whether that order is good
    is what the measure below decides; the builder does not add normalization or rank
    fusion.
  - **Present tense:** DESIGN 10's search paragraph, the `search.py` module docstring, the
    `_TIER_*` comment, and the `search()` and `_ask` docstrings describe the two tiers and
    the near-tie rule. `_curated_priority` and H5's strict order are removed.
  - **Measure, before merging (decision rule set now):** (1) F80's coding key on the same
    snapshot, compared with F80's baseline entry by entry; (2) LongMemEval-S pipeline
    `--rescore` of the kept sandboxes (`sample60-rerun.jsonl`, 37 questions, and
    `diag6-f73.jsonl`, 6, two of them shared), run before and after on the same sandboxes,
    with no model calls. F79 merges when the coding
    key's recall@5 and MRR do not fall and no entry whose answer is on a page drops out of
    the top 5. LongMemEval is confirmation only and is never tuned against. If the coding
    key falls, F79 does not merge and the results go to Ted.
  - **Tests** in `tests/test_search.py`: a strong evidence match outranks a weak page
    match in the same tier; a page within 10% of an evidence row outranks it; the current
    project's row and a global page interleave by score; another project's row ranks after
    every tier-0 row whatever its score; a collection's `LIMIT` keeps the rows the merge
    needs. Review checkpoint: yes (search order; lead reviews in full).

- **F81 Query words: any word, without question filler** (ruling 2026-10-10; amends H5's
  conjunction-before-disjunction; builds after F82 merges). `aft search` first asks for rows
  holding every query word and falls back to any word only when nothing holds them all. An
  agent's question carries filler ("where does the switchboard project live"), so the
  all-words query either matches nothing useful or matches a few rows that happen to hold
  every filler word, and the fallback never runs. On F80's coding key (2026-10-10 baseline,
  recall@10 0.33), removing filler words and asking for any word reached 0.40 in an
  in-memory trial.
  - **Filler words:** a fixed module constant in `search.py`, dropped from the query's bare
    words before the match: a, an, and, are, as, at, be, by, can, could, did, do, does, for,
    from, how, i, in, is, it, its, me, my, of, on, or, our, should, that, the, there, this,
    to, was, we, were, what, when, where, which, who, why, will, with, would, you, your.
    Matching is case-insensitive. Negations (no, not, never) are kept. A quoted phrase is
    kept whole. When every bare word is filler, the words are kept as typed. The list is
    set here and is not tuned against the key.
  - **Any word:** the remaining words are joined with OR and ranked by BM25 (with F79's
    tiers and near-tie rule); a row holding more of the words scores higher by BM25's own
    sum. The all-words first pass and its fallback are removed. Temporal parsing, the
    per-session evidence collapse and `--archive`/`--reports` are unchanged.
  - **Scope:** the query side only. The index, its tokenizer and `aft inject` are
    unchanged. Questions that share no word with their answer ("budgeting app" against
    "Actual budget") stay out of reach; that is a separate question for Ted, not this task.
  - **Present tense:** DESIGN 10's search paragraph, the `search.py` module docstring, and
    the `_ask` and `_to_fts5_terms` docstrings.
  - **Measure, before merging (decision rule set now):** F80's coding key on the
    2026-10-10 snapshot, against the F82 merge's run on the same snapshot and key, entry by
    entry; and the LongMemEval-S `--rescore` pair over F79's sandboxes. F81 merges when the
    coding key's recall@5 and MRR do not fall and no entry found in the top 3 before drops
    out of the top 5. LongMemEval is confirmation only. If the coding key falls, F81 does
    not merge and the results go to Ted.
  - **Tests** in `tests/test_search.py`: a question with filler words finds a row holding
    only its content words; a row holding more content words outranks one holding fewer; a
    query of filler words only still searches them; a quoted phrase holding a filler word
    is matched as a phrase; a negation word is kept. Review checkpoint: yes (search order;
    lead reviews in full).

- **F82 One index row per page line** (ruling 2026-10-10; follows F79; DESIGN 10, `index.py`
  row table). A `pages` or `archive` row today holds a whole `##` section, and a project's
  Facts section runs to about a hundred lines, so BM25's length normalization scores a
  page's best match near -3 against -9 for a short evidence entry that says the same thing;
  F79's 10% near-tie rule cannot close that gap. On F80's coding key, "where is firstmate
  cloned" fell from rank 2 to 7 under F79 for this reason.
  - **Rows:** each live curated line of a `pages` section is its own row, and each resting
    line of an `archive` section likewise. A row keeps the page's title and description
    and the section's heading in their columns, and its body is the line's statement alone
    (the tag, date and footnotes stay out, as they do today). The row key adds the line's
    position in its section, so two lines under one heading are two rows. Prose that is not
    a fact line (a page's text outside its fact sections that is indexed today) stays one row
    per heading. `digests` and `reports` keep one row per heading section.
  - **Search:** unchanged in its rules. A page answers once per query at its best line
    (the existing per-path de-duplication), and its snippet is drawn from that line.
  - **Rebuild:** `index.BUILD_STAMP` rises to `rows-7`; `index.update` already rebuilds
    from files when the recorded stamp differs, so an existing index is never left holding
    section rows beside line rows.
  - **Unchanged:** the BM25 column weights, F79's tiers and near-tie factor, `aft inject`,
    `aft eval`'s pass/fail mode, and LongMemEval's F72 line matching (a page hit still names
    its section by heading, and the matching picks the line inside it).
  - **Present tense:** `index.py`'s module docstring and row table, and DESIGN 10's
    collection table where it says what a pages row holds.
  - **Measure, before merging (decision rule set now):** F80's coding key on the
    2026-10-10 snapshot, reindexed, against F79's run (`f79-f216b1e.json`), entry by entry;
    and the LongMemEval-S `--rescore` pair over F79's 41 sandboxes. F82 merges when the
    coding key's recall@5 and MRR do not fall, no entry found in the top 5 under F79 drops
    out of it, and "where is firstmate cloned" returns to the top 5. LongMemEval is
    confirmation only. If the coding key falls, F82 does not merge and the results go to
    Ted. F81 builds after F82, so each is measured alone.
  - **Tests** in `tests/test_index.py` and `tests/test_search.py`: a two-line section
    gives two rows with distinct keys; a struck line rests in `archive` as its own row; a
    page's matching line outranks a long section's weaker match from another page; one
    page still answers once; an index from the `rows-6` build is rebuilt on update; the
    rebuild-equals-update test still holds. Review checkpoint: yes (index and search order;
    lead reviews in full).

- **T38 Coding-search key to about 100 entries** (ruling 2026-10-11; lead's task, lands
  before F83's measurement). F80's key holds 40 entries, so one entry moves recall by 2.5
  points, too coarse to choose between F83's variants.
  - **Where:** `~/src/aft-eval-data/search/coding.yaml`, over a fresh snapshot
    (`snapshot-<date>/`, taken with `eval_search.py snapshot` after F81), never in git. The
    40 entries stay; new ones are added.
  - **Sources:** what a session actually needed and asked: user turns and agent questions in
    the evidence archive, and the exported coding sessions. A question is worded as an agent
    would search for the fact, never rephrased from the answer line. Each expected item is
    checked present in the snapshot.
  - **Spread, at least:** 25 entries whose answer is on a page, 25 only in a digest, 25 only
    in evidence; 15 whose words share no content word with the answer (synonym or paraphrase
    questions, marked `# paraphrase`); 10 whose words also match another project's memory.
  - **Baseline:** the lead runs today's search over the new key and snapshot and keeps the
    JSON beside the key.

- **F83 Search by meaning without a second model: a measured spike** (ruling 2026-10-11;
  after T38). Most coding-key misses share no word, or only a weak one, with their answer.
  The user rules out running a second model; vectors, graphs and other tools are allowed if
  they help. This spike measures three ways to match by meaning and changes no production
  code: what it builds is evaluation tooling, and adopting a variant is a later task Ted
  rules on.
  - **Seam:** `eval/search_spike.py` ranks a question through a named variant and returns
    hits as `aft.search.search` does, so `aft.eval.rank` (F80) and the LongMemEval
    pipeline `--rescore` (F72) can score any variant. Both gain a `--variant NAME` option;
    the default is today's search, unchanged.
  - **Variant `rerank`:** today's search fetches its top 30; the configured model orders them
    by whether each answers the question, given the question and each hit's text (F80's
    `_hit_text`, cut to 600 characters), and the top 10 of that order are the result. The
    answer is schema-constrained JSON naming candidate numbers (R9's form). The prompt lives
    in `eval/` and ships nowhere. Record tokens and wall time per question.
  - **Variant `expand` (doc2query):** for each page line and digest section of a snapshot,
    the configured model writes up to 8 short search phrases a person might use to look for
    it (synonyms, the question it answers), cached once per snapshot in
    `<snapshot>/expansions.json` keyed by row. A variant index in a copy of the snapshot
    appends a row's phrases to its body; search is otherwise today's. Evidence rows get no
    phrases. Record total tokens and generation time.
  - **Variant `lsa`:** TF-IDF over every indexed row of the snapshot (Porter-stemmed as the
    index stems), reduced by truncated SVD to 256 dimensions; a question ranks rows by
    cosine similarity, and that ranking is fused with today's by reciprocal rank fusion (k
    = 60). It runs with numpy supplied by `uv run --with numpy`, so afterthought gains no
    dependency during the spike.
  - **Combinations:** `expand+rerank`, and `lsa+rerank` if `lsa` alone beats today's search.
  - **LongMemEval:** `rerank` and `lsa` over the 41 kept sandboxes; `expand` over the six
    diagnostic sandboxes (`diag6-f73`) first, and over all 41 when its generation time per
    sandbox, measured on the six, puts the whole run under four hours.
  - **Report (lead), with the decision rule set now:** a table of every variant on T38's key
    (recall@1/5/10 and MRR, overall and for the `# paraphrase` entries) and on LongMemEval
    (recall_any@1/5, recall_all@10), with tokens and time. A variant is proposed to Ted for
    production when the key's recall@5 rises by at least 5 points over today's search, its
    MRR and LongMemEval recall_any@5 do not fall, and, for any variant that calls the model
    at search time, the p90 search time stays under 3 seconds. The proposal names what
    production would need (where expansions live so files stay the truth, the fallback
    when the endpoint is down, and any dependency such as numpy).
  - **Tests** in `tests/test_eval.py` with the fake endpoint: each variant runs end to end
    on a fixture vault; `rerank` keeps only candidates it was given and falls back to
    today's order on an invalid answer; `expand` reads its cache and calls the model only for
    rows missing from it; the default variant equals `aft.search.search`. The `lsa` test is
    skipped when numpy is absent. Review checkpoint: no (evaluation tooling; the adoption
    task that may follow gets a full review).

- **F84 `aft search` orders its candidates with the model** (ruling 2026-10-11; adopts F83's
  `rerank`, report `.board/f83-report.md`; DESIGN 10). On T38's key, reranking raised
  recall@5 from 0.48 to 0.62 and MRR from 0.41 to 0.53, with p90 search time 1.83 s and about
  3,900 tokens a search.
  - **Where:** the `aft search` command only (`search_command`, and so the agents' own
    searches). `aft.search.search()` gains `rerank: bool = False`; every other caller,
    `aft inject`, the memory block, the dream and `aft eval`'s pass/fail mode, stays
    model-free. `aft eval --ranks` follows the setting, with `--no-rerank` to measure
    without it.
  - **How:** today's search fetches its top 30; the configured model gets the question and
    each candidate's text (the section a page or digest hit names, an evidence hit's archived
    entry, cut to 600 characters; the text F80's `_hit_text` reads, moved into `search.py`
    so production does not import from `eval`) and answers schema-constrained JSON naming
    candidate numbers in order (R9). The result is that order, then any candidates it left
    out in today's order, cut to `--limit`. The prompt is `src/aft/prompts/rerank.md` with
    `rerank.schema.json`, worded from F83's.
  - **Fallback, never an error:** when the setting is off, the endpoint is in cooldown or
    unreachable, the call exceeds `search.rerank_timeout_seconds`, the answer is invalid or
    names no candidate, or today's tokens have reached `work.daily_token_budget`, the
    command prints today's order exactly as it does now. Nothing is printed about the
    fallback on stdout; a failure is logged as other model failures are.
  - **Settings:** `[search]` with `rerank = true` and `rerank_timeout_seconds = 5`
    (`config.py`, `aft init`'s template, `docs/configuration.md`).
  - **Tokens:** the call goes through `aft.model` like every other, so it is recorded in
    `usage.jsonl` and counts against the daily budget.
  - **Present tense:** DESIGN 10's search paragraph (search orders its top candidates with
    the model when it can, and falls back to BM25 order), `docs/commands.md`,
    `docs/configuration.md`, the `search.py` docstrings.
  - **Measure, before merging (decision rule set now):** T38's key on its snapshot through
    the production path: recall@5 at least 0.60 and MRR at least 0.50 (F83's `rerank`: 0.62,
    0.53), p90 search time under 3 s; with `--no-rerank` the results equal F81's run
    entry by entry. If it falls short, F84 does not merge and the results go to Ted.
  - **Tests** (`tests/test_search.py`, fake endpoint): the model's order is applied and
    omitted candidates follow in today's order; an invalid answer, an unreachable endpoint, a
    timeout, a spent budget and the setting off each give today's order; `search()` without
    `rerank=True` makes no model call; `aft inject` makes none; the prompt-shape test pins
    the prompt. Review checkpoint: yes (search order and a new model call; lead reviews in
    full).

- **F85 Search phrases, kept as vault files** (ruling 2026-10-11; adopts F83's `expand`;
  after F84; DESIGN 10 and 8). On T38's key, phrases with reranking raised paraphrase
  recall@5 from 0.25 (rerank alone) to 0.38. Phrases are model output, so they are files in
  the vault, never index-only: the index stays a cache rebuilt from files with no model.
  - **What:** for each active line of a `pages` row and each section of a `digests` row,
    up to 8 short phrases a person might search with (synonyms, the question it answers),
    written by the configured model from `src/aft/prompts/phrases.md` (schema-constrained,
    R9), one row per call: asked for twenty rows at once the model restated each row's own
    words, and paraphrase recall@5 stayed at 0.25 against 0.38 at one row a call.
  - **Where they live:** `phrases/<the page's or digest's own path>.json` in the vault,
    mapping a key to its phrases. A page line's key is a hash of its statement text, so an
    edited line has no phrases until new ones are written; a digest section's key is a hash
    of its heading and text. The files are committed with the run that writes them.
  - **When:** a step of `aft run` writes phrases for every indexed row that has none, under
    the daily token budget and the endpoint's cooldown like other background work, and
    removes entries whose key no longer matches an indexed row (a withdrawn, superseded or
    edited line). The first run backfills the current vault (about 800 rows, 320k tokens).
  - **Index:** a `phrases` column on `pages` and `digests` rows, weighted as body text, filled
    from the phrase files; `BUILD_STAMP` rises. A hit's snippet is drawn from the row's own
    text, never from its phrases.
  - **Never memory:** phrases are never injected, never shown as a fact, never read by the
    digest, dream, review or reports, and never a source. `aft read` does not show them.
  - **Forget and redaction:** `aft forget session` removes the phrases of every line and
    digest section it removes or rewrites; `aft forget pattern` and `capture.redact_patterns`
    apply to phrase files as to every other vault file (DESIGN 8: forgetting reaches
    everything afterthought controls). List each forget and redaction path and how it
    reaches phrase files in the PR body.
  - **Present tense:** DESIGN 10 (the phrases column and where phrases come from), DESIGN 8
    (forget reaches phrase files), `docs/vault.md` (the `phrases/` directory).
  - **Measure, before merging (decision rule set now):** T38's key on a snapshot taken after
    the backfill, with F84's reranking: paraphrase recall@5 at least 0.31 and overall recall@5
    and MRR not below F84's run. If it falls short, F85 does not merge and the results go to
    Ted.
  - **Tests:** phrases are written for rows that lack them and removed for rows that left;
    an edited line loses its stale phrases; a rebuild from files reproduces the phrases column
    with no model call; snippets never show phrases; inject and `aft read` never show them;
    forget session and forget pattern remove or redact them; a spent budget stops the step.
    Review checkpoint: yes (privacy: forget and redaction; lead reviews in full).

- **F86 Search has its own token budget** (ruling 2026-10-11; follows F84). `aft search`'s
  rerank (about 4,000 tokens a search) draws on `work.daily_token_budget`, the same budget
  the digests, dream and phrases use, so a day of heavy searching can stop the digests, and
  a day of heavy digesting turns reranking off.
  - **Ledger:** each line `aft.model` appends to `usage.jsonl` names its purpose: `search`
    for the rerank call, `work` for everything else. A line written before F86, with no
    purpose, counts as `work`.
  - **Budgets:** `search.daily_token_budget` caps the day's `search` tokens and
    `work.daily_token_budget` caps the day's `work` tokens; neither counts the other's. The
    rerank checks the search budget, and digests, dream and phrases check the work budget,
    each through `tokens_used_today(config, purpose=...)`. Default for the new setting:
    200,000.
  - **Reset and report:** `aft budget` prints both counts against both caps; `aft budget
    reset` resets both, as one reset does today.
  - **Present tense:** DESIGN 10's search paragraph (the search budget), DESIGN 13's budget
    text, `docs/configuration.md`, `docs/commands.md`, and `aft init`'s template.
  - **Tests:** search tokens leave the work budget untouched and the reverse; a spent search
    budget turns reranking off while digests continue; a spent work budget stops digests
    while reranking continues; a pre-F86 ledger line counts as work; `aft budget` shows both;
    a reset resets both. Review checkpoint: no.

- **T39 Budgets and the rerank timeout must be positive** (ruling 2026-10-12; follows F86).
  `config.load` accepts `work.daily_token_budget = -5`, and a zero or negative budget stops
  that work every day without saying why. `config.load` raises `ConfigError` naming the key
  when `work.daily_token_budget` or `search.daily_token_budget` is not a positive integer, or
  `search.rerank_timeout_seconds` is not a positive number, beside the existing
  `dream.interval_minutes` check. Tests in `tests/test_config.py`: zero and a negative value
  for each key are refused with the key named; the defaults load. Review checkpoint: no.

- **T40 The Claude reader skips sidechain records** (ruling 2026-10-12; ROADMAP finding L5).
  A Claude Code record marked `"isSidechain": true` is a subagent's conversation, and its
  `user` messages are the main agent's prompts, not the user's words; read as user turns they
  could ground a `[stated]` fact the user never said (R2). Today's Claude Code writes
  subagents to `subagents/agent-*.jsonl`, which capture never reads (the sweep globs
  `*/*.jsonl`), and none of the user's 1,623 main transcripts holds an inline sidechain
  record, so this guards against a layout older or future versions use.
  - **Reader:** `aft.readers.claude` yields no entry for a record whose `isSidechain` is
    true; every other record reads as today.
  - **Tests** (`tests/test_readers.py` or the Claude reader's tests): a transcript holding a
    sidechain `user` and `assistant` record yields neither; the main records around them read
    unchanged; capture's sweep does not pick up a `subagents/agent-*.jsonl` file. Review
    checkpoint: no.

- **M15 `aft forget pattern` reaches the library's extracted text** (ruling 2026-10-12;
  ROADMAP finding M15; DESIGN 8 and 11). Library rows are built from the extraction cache
  `<home>/library/<hash>.json`, keyed by the import-time hash, and refreshed only when a
  collection's recorded file hash changes, which a redaction never does; so a redacted secret
  keeps answering `aft search`. The cache keeps it too, and today's rewrite of library `*.md`
  sources breaks their recorded hashes although sources are read-only once imported.
  - **Cache:** `forget pattern` redacts every chunk text in `<home>/library/*.json` with the
    same replacement, a cache no manifest lists included. A text redaction that would break a
    listed cache's JSON goes through the import's writer instead, which always leaves it
    parseable, so no listed cache keeps a match; an unlisted cache whose redaction would
    break it is refused and named.
  - **Index:** each library document whose cached text changed has its rows and recorded hash
    dropped, so the same command's index update rebuilds them from the redacted cache. A
    document with no cache whose extracted text matches gets a redacted cache written first,
    so the rebuild never extracts the unredacted source.
  - **Sources stay read-only:** files under `<vault>/library/` are no longer rewritten. The
    command's report names each library source whose text (read directly, or through its
    extraction for a PDF) still holds a match, and says that removing it means deleting or
    re-importing that file.
  - **Present tense:** DESIGN 8's `aft forget pattern` bullet (it reaches the library's
    extracted text and search, and names library sources that still hold a match), DESIGN 11
    where it describes the cache, `docs/commands.md`.
  - **Tests:** a pattern found in a cached library chunk no longer answers `aft search` after
    `forget pattern`; the cache holds `[redacted]`; a library source file is unchanged and
    named in the report; an uncached PDF or text document gets a redacted cache before the
    rebuild; a redaction that would break a listed cache's JSON goes through the writer; an
    unlisted cache is redacted too. Review
    checkpoint: yes (privacy: forget; lead reviews in full).

- **M13 Dates in a query are read only when the query says so** (ruling 2026-10-12; ROADMAP
  finding M13; DESIGN 10). `aft.temporal` takes a bare weekday or its abbreviation as a date:
  "sun microsystems", "sat solver" and "wed to the schema" each lose a word to a one-day
  filter, "rotate key before friday" keeps "before" and loses "friday", and "this friday"
  resolves to a day still ahead. Only evidence is filtered by time, and a query that is only a
  time expression returns rows in no useful order.
  - **Weekdays need a cue:** a weekday, full name or abbreviation, is a date only after `on`,
    `last`, `past`, `this`, `since`, `before` or `until`; the cue and the weekday are both
    removed from the words. A bare weekday stays a search word. `before`/`until <weekday>`
    sets the end of the window as `before`/`until <date>` does today.
  - **Never ahead:** a resolved day later than today (`this friday` on a Monday) becomes the
    most recent past occurrence; a window's end is never later than now.
  - **Digests have dates:** a digest row records the date its file name carries
    (`<date>-<agent>-<session>.md`) in the `time` column, so a time window filters digests as
    it filters evidence; pages carry no time and are never filtered. `BUILD_STAMP` rises.
  - **A query that is only time:** with no words left, the result is the window's digest rows,
    newest first, then its evidence, newest first, cut to `--limit` (no BM25, no rerank).
  - **Unchanged:** ISO dates, `today`/`yesterday`, `last|past N days|weeks|months|years`,
    `since`/`before` with an ISO date or relative day, and ranges keep today's meaning.
  - **Present tense:** DESIGN 10's sentence on time words in a query, the `temporal.py` and
    `search.py` docstrings, `docs/commands.md`.
  - **Tests** (`tests/test_temporal.py`, `tests/test_search.py`): "sun microsystems", "sat
    solver", "wed to the schema" and "rotate key before friday" set no window and keep every
    word; "on friday", "last tuesday", "since mon" and "before friday" set one and remove the
    cue and weekday; "this friday" asked on a Monday resolves to the past Friday; a digest row
    carries its file's date and a window excludes a digest outside it; a pages row answers
    whatever the window; a time-only query lists the window's digests newest first. Review
    checkpoint: no.

- **T41 Recent memory shows the newest lines; a settled replacement is grounded** (ruling
  2026-10-12; ROADMAP findings L1 and L4).
  - **L1, `inject.py` `_recent_part`:** the short-term layer gathers `recent.md`'s active lines
    in section order and keeps the first `MAX_RECENT_LINES`, so a page holding more than ten
    shows its oldest lines and drops the newest. It keeps the ten with the newest dates (a
    line with no date counts as oldest; ties keep page order), then shows them in the
    overview's section order as today. The heading's "newest" date is unchanged.
  - **L4, `notes.settle` with `replace`:** the replacement statement is written as the user
    typed it and tagged `[stated]` when one of the note's sources is the user's, but nothing
    checks that it says only what those sources say. It now passes R2's grounding check
    (`digest.ungrounded`) against the joined text of the quotes it is cited to, as every
    `[stated]` line does (DESIGN R2: the statement holds no name or number its sources do not
    say). A statement with an ungrounded name or number is refused with a `NoteError` that
    lists them and says to reword it or settle with `--keep` or `--withdraw`; nothing is
    written. Its date rule is unchanged here.
  - **Tests:** a `recent.md` with twelve dated lines injects the ten newest, in section order;
    an undated line drops first; a replacement holding a number the quotes do not say is
    refused and the page is unchanged; a grounded replacement is written as today. Review
    checkpoint: yes (L4 applies R2 to a new writer; lead reviews in full).

- **T42 `aft search` reranks its top 60** (ruling 2026-10-12; follows F84). Measured on T38's
  key with the endpoint idle and the 5 s timeout: a pool of 30 reaches recall@5 0.60 and MRR
  0.53 (p90 1.56 s); a pool of 60 reaches 0.67 and 0.58 (p90 2.68 s, one fallback in 103);
  100 adds nothing over 60. `search.RERANK_CANDIDATES` becomes 60, at about 7,600 tokens a
  search. Present tense: the `search()` docstring and DESIGN 10's "top 30". Tests: a reranked
  search shows the model a pool of 60 when 60 candidates exist. Review checkpoint: no.

- **T43 A settled replacement cites the settle as its own source** (ruling 2026-10-12;
  follows T41; DESIGN 6 and 9). `aft settle --replace` dates the new line by the newest of the
  note's quotes, so evidence said after those quotes but before the user settled can reach the
  dream later and count as newer than the user's decision. The settle commit is itself the
  user's edit, and hand edits already cite their commit as a source (`aft://edit/<sha>`,
  verified against the commit's added lines and dated by it, `tag_hand_edits`).
  - **Two commits:** the settle commits as today (the replacement line with the note's
    sources, the note to the page's archive). A second commit, `aft: cite settle <sha> on
    <where>`, adds a source `aft://edit/<sha>` of that first commit to the new line, whose
    quote is the line's statement as written, and dates the line with the first commit's UTC
    date. The line's tag is unchanged (T41's grounding check still applies first). If the
    second commit cannot be made, the first stands and the command says so.
  - **No new kind of source:** the dream verifies and dates the new source through the
    existing `aft://edit/` path; nothing in R1 to R10 changes.
  - **Reporting and undo:** `aft settle` prints both commit hashes. The aft skill
    (`integrations/skills/aft/SKILL.md`, both places it says `git revert`) and
    `docs/commands.md` say a replacement is two commits, reverted newest first.
  - **Present tense:** DESIGN's notes text where settling a note by replacement is described.
  - **Tests:** a replacement line cites `aft://edit/<first sha>` with the statement as its
    quote and carries the settle day's date; the dream's lookup verifies that source and dates
    it by the commit; a digest dated between the note's quotes and the settle day does not
    count as newer evidence than the line; reverting both commits restores the page and the
    note; a failed second commit leaves the first commit's line valid. Review checkpoint: yes
    (sources and dates; lead reviews in full).

- **T44 A vault commit carries only its own paths** (ruling 2026-10-12; ROADMAP finding L8).
  `vault.commit` runs `git add -A -- <paths>` then `git commit`, which commits everything
  staged in the vault, so a change someone else staged (an Obsidian plugin, a hand `git add`)
  rides along in afterthought's commit under its message. It commits with `git commit --only
  -- <paths>` (removals included), and decides whether there is anything to commit by
  `git diff --cached --quiet -- <paths>`, so another staged path neither rides along nor turns
  an empty commit into a real one. Tests in `tests/test_vault.py` (or where commit is tested):
  a file staged by hand outside `paths` stays staged and out of the commit; a removal in
  `paths` is committed; nothing staged within `paths` makes no commit even when another path
  is staged. Review checkpoint: no.

- **F87 Recall on each prompt** (ruling 2026-10-12; DESIGN 10 and 15, which list "recall on
  each prompt" after v1). An agent reads the block a session starts with and rarely runs
  `aft search` itself (in the user's 300 latest Claude sessions, 3 searches), and no part of
  the system may depend on the working model choosing to use memory. Each prompt therefore
  brings back the few memory lines that bear on it, beside the prompt.
  - **Hooks:** Claude Code's `UserPromptSubmit` (payload field `prompt`; context added as
    `hookSpecificOutput.additionalContext`, capped by Claude Code at 10,000 characters) through
    `aft hook claude UserPromptSubmit`, installed by `aft install claude` beside the existing
    groups with the same 5 s timeout; pi's `before_agent_start` through the extension, as a
    section of its own beside the block (the builder confirms which field of pi's event holds
    the prompt and records it in the extension's comment).
  - **Query:** the prompt with any `<pasted_content ...>` block, fenced code and the capture
    markers (`aft off`, `aft on`, "off the record", "on the record") removed, cut to its first
    32 content words (F81's filler list applies). A prompt left with fewer than 3 content words
    ("yes", "do it") recalls nothing. The search runs as `aft search` does from the session's
    working directory, BM25 only by default (`recall.rerank = false`): a recall runs on every
    prompt and must not add seconds to it.
  - **What is added:** at most `recall.max_hits` (3) hits whose matched row holds at least
    min(3, n) of the query's n content words, none of them a line the session's start block
    already holds, each one line (path, heading, the matched line or snippet), under the
    factual heading "Memory that may bear on this prompt (from aft search; check before relying
    on it):", the whole at most `recall.max_chars` (1,500). Nothing passes the bar: nothing is
    printed. Only curated memory is shown (page lines, digest sections, the library), never an
    archive entry or the session's own digest; the bar counts a word on its first five letters,
    as the index stems; a worker session (`capture.worker_paths`) recalls nothing.
  - **Privacy:** the prompt's text is never written anywhere by recall (no log, no archive, no
    index); a failure is logged as other hook failures are, without the prompt. A session whose
    capture is off still recalls (recall stores nothing).
  - **Settings:** `[recall]` with `enabled = true`, `max_hits = 3`, `max_chars = 1500`,
    `rerank = false`, validated positive like T39's.
  - **Present tense:** DESIGN 10 (recall on each prompt, its bar and its limits) and DESIGN 15
    (remove it from the after-v1 list), `docs/commands.md`, `docs/configuration.md`, the aft
    skill's description of what a session receives.
  - **Measure, before merging (decision rule set now):** on T38's key, each question as a
    prompt: recall@3 at least today's BM25 recall@3 (0.44) after the bar, and the share of
    prompts that add anything reported; hook time p90 under 300 ms with BM25 only (and
    reported with `rerank = true`); on 50 real user prompts sampled from the archive, the lead
    reads every recall added and reports how many were useful, neutral or noise. Ted decides
    whether the bar or `max_hits` changes before merging.
  - **Measured, ruling 2026-10-12** (`.board/f87-report.md`): with the archive included, key
    recall@3 0.456 but more than half of the blocks on 41 real prompts were noise (the user's
    own earlier prompts, tool output). Ted ruled curated memory only and no recall for worker
    sessions. As merged: the start block alone holds 0.369 of the key's answers, start block
    plus recall 0.466; 18 of 41 real prompts add a block (3 useful, 5 neutral, 10 noise, six
    of them firstmate briefs from pi sessions outside `capture.worker_paths`); hook p90 275 ms.
  - **Tests:** a prompt that matches adds at most `max_hits` hits within `max_chars`, a short
    prompt adds nothing, a pasted block and code fence do not reach the query, a line already in
    the start block is not repeated, the prompt is in no file afterwards, a failure prints
    nothing and exits 0, `aft install claude` adds the `UserPromptSubmit` group and
    `--uninstall` removes it. Review checkpoint: yes (a new path into every session's context;
    privacy of the prompt; lead reviews in full).

- **F88 An order to do work is a request, not memory** (ruling 2026-10-12). Memory holds
  `[stated]` lines that are task orders the user gave or approved: "Spec L5 and M15 and start
  them on pi.", "Merge F85 and spec the search token budget as a follow-up task.", "Make the
  Node-RED change and commit both." `turns.md` labels a yes to a proposal a decision, so "yes,
  spec L5 and M15 and start them on pi" became a decision line, and the decide call then named
  a later, unrelated order ("Switch to pool 60 and start it on pi.") a conflict with it, which
  reached the user as a note to settle. Such a line fails the keep-test: once the work is done,
  no future session acts differently for knowing it was ordered.
  - **`turns.md`:** a yes to a proposal is a decision only when what it approves stays true
    after the turn (a design choice, a setting, a rule, a plan the user adopts); a yes to doing
    a piece of work now (spec, build, start, merge, run, fix, commit something) is a request,
    as the same words without the proposal would be. One example of each in the prompt,
    neutral and invented.
  - **`digest.md`, `decide.md` and `review.md`:** the never-a-fact list gains "a one-time
    order to do a piece of work, given or approved by the user, whether or not it was carried
    out", and says a standing rule, a permission and the choice an approval settles are not
    such orders (without that sentence, `aft review` named a standing rule and a standing
    permission as orders); `decide.md` adds that two orders to do different pieces of work
    are about different things, never a supersede or a conflict.
  - **Measure, before merging (decision rule set now):** `scripts/eval_digest.py score-turns`
    on key-turns.json with the local model, base against this change: `stated_wrong` falls and
    durable items found do not fall beyond run-to-run noise (two runs of each). The lead also
    runs `aft review afterthought` on a copy of the vault before and after and reports how many
    order lines each names. Merge only if the rule holds.
  - **Not in this change:** withdrawing the order lines memory holds today. After merge the
    lead offers Ted `aft review <project>` and he chooses what to withdraw.
  - **Tests:** prompt-shape tests updated for the three prompts. Review checkpoint: yes
    (prompt change; lead reviews in full).

- **T45 A settled replacement may rest on the line's own quotes** (ruling 2026-10-12; follows
  T41). `aft settle --replace` grounds the new statement only in the note's quotes, so a
  replacement that corrects one detail of a line is refused for every name the line's own
  quotes hold and the note's do not. Settling the note under "The lead must personally read
  only the trust-rule and privacy parts of PRs (R1-R9, ...)", whose note quote is "None of the
  trust rules R1 to R10 change.", refused "trust-rule, PRs, R1-R10"; the user had to keep a
  line they wanted corrected.
  - **`notes.settle` with `replace`:** the evidence is the note's verified non-worker sources
    together with the line's own verified non-worker sources (the same `verified` and
    `origin != WORKER` filter), deduplicated by id. R2's check (`digest.ungrounded`) runs
    against the joined quotes of that whole set, and the replacement cites the whole set,
    line sources first. The tag is `[stated]` when any of them is the user's. The refusal
    when no note source verifies stays: a replacement still needs new evidence from the note.
    T43's second commit and its date are unchanged.
  - **The aft skill:** when drafting a replacement, write each name and number as the quotes
    write it (`R1 to R10`, not `R1-R10`), since R2 matches a token holding a symbol exactly.
  - **Tests:** a replacement holding a name only the line's quote says is accepted and cites
    both the line's and the note's sources; a name in neither is still refused; a worker
    session's line quote does not ground it; a note with no verifying source is still refused
    whatever the line's quotes say. Review checkpoint: yes (R2 for a writer; lead reviews in
    full).
- **M16 The digest evals score correctness, not word overlap** (ruling 2026-10-13; ROADMAP
  finding M16). `scripts/eval_digest.py` pairs a run fact with a key item when their quotes
  share 60% of their words or their statements 40% of their content words, and a pair counts
  as found whatever the statement says: a right quote under a wrong statement scores correct.
  `score_turns`, the merge rule of F77, F78 and F88, counts a key item found when any run fact
  overlaps it, so one run fact satisfies several key items. `[observed]` facts are counted and
  never judged. The key was labelled by backends under test, and every prompt change since
  F73 was tuned on the same items it was scored on. `score_block` (`current`) finds a phrase
  only as an exact substring.
  - **One to one:** `score_turns` pairs key items and run facts through `match_facts`, the
    strongest pairs first, as `score_session` already does, so a run fact answers at most one
    key item. A `[stated]` fact left unpaired is wrong as today.
  - **Each pair is judged.** A judge call on the configured endpoint, temperature 0,
    schema-constrained, sees the key statement, the key quote and the run statement, never
    which backend wrote it, and answers one question: does the run statement say what the
    key statement says? The schema puts `differs` (the words that differ, empty when none)
    before `verdict`: `same`; `narrower` (part of the key's claim, contradicting nothing);
    `broader` (all of the key's claim and more beside it, changing none of it); `different`
    (changes, contradicts or replaces the key's claim). `same`, `narrower` and `broader` are
    found, the last two also counted on their own; `different` is a wrong statement, and
    the key item counts as missed. A name, number, date or negation put in place of the
    key's own is always `different`, however much detail surrounds it. The judge prompt
    lives beside the script, not among the shipped prompts.
  - **Unheld names:** the names and numbers of the run statement that neither the key text
    nor the run fact's own evidence (its quote, and the proposal's words for an approval)
    holds are recorded on the pair as `unheld` (`digest.ungrounded`), for the reader; they
    do not decide the verdict. A summary keeps one quote per fact while the digest grounded
    the statement in the whole turn, so a true detail can be missing from the quote; as a
    verdict this check failed correct statements (`captain.md`, a tester's name).
  - **Verdict cache:** `<data>/judge-cache.json`, keyed by a hash of the judge prompt, the
    model, the key statement, the key quote and the run statement, so scoring a stored run
    twice calls the model once and gives the same numbers.
  - **The judge is checked before its verdicts count** (decision rule set now). The lead
    writes `<data>/judge-check.json`: 40 planted wrong (key statements with one name,
    number or negation changed, half of them hidden inside added detail), 40 planted same
    (rewritten in other words) and 40 planted broader (a true detail added). The judge
    passes at 38 of 40 planted wrong judged `different`, 36 of 40 planted same judged `same`
    or `narrower`, and 36 of 40 planted broader judged found. The lead also reads 40 real
    judged pairs from runs not read while building the judge and agrees with at least 36. A judge that fails is reworked
    (prompt, question split) before any score built on it is reported.
  - **`[observed]` precision:** each `[observed]` fact is judged against its own cited quote
    by the same judge (does the quote say what the statement says), and the report gives
    observed facts supported, narrower, broader (the statement says more than its quote)
    and unsupported. Observed recall needs a key of agent
    entries, which does not exist; it is not in this change.
  - **Held-out split:** the key's labelled turns split by a hash of the turn id into `dev`
    and `held-out`, about half each, frozen in `<data>/split.json` (it names real sessions,
    so it stays out of git). `score` and `score-turns` report both splits; a prompt change is
    tuned on `dev` and its merge rule reads `held-out`.
  - **`current`:** a scenario names, for each current and superseded value, the tokens that
    tell it apart (`settle` against `yes` in `replaced-rule`). A value is present when a block
    line holds all its distinguishing tokens, matched casefolded with punctuation set aside,
    and the judge, given the line as the key, finds the value in it. Word overlap alone never counts, because a
    current and a superseded value share most of their words.
  - **Each report records** the prompt hash of the digest prompts, the judge prompt hash, the
    model, the temperature, the split, and each backend's prompt shape. The Claude and Gemini
    backends embed the schema in the prompt text because their CLIs cannot enforce one (a
    standing decision), so a report says which shape each backend ran.
  - **Re-scoring:** the lead re-scores the stored runs under `answers/` (the F77, F78 and
    F88 runs) with the new scorer and reports old and new numbers side by side in the PR,
    and reads every pair the old scorer counted found and the judge counts different. The
    figures quoted in earlier entries ("42 of 76", F88's `stated_wrong` 4 to 2) are old-scorer
    numbers and are not compared with new ones.
  - **Measured, ruling 2026-10-14:** judge check 40/40 wrong, 40/40 same, 40/40 broader;
    the lead agreed with 37 of 40 verdicts on the Claude runs (the 3 misses lenient: "PLT
    Focus" for the user's "PL Focus" passed inside longer statements twice). `current`: 5 of
    5 scenarios pass. Split: 35 dev and 41 held-out key items. The local runs, old scorer
    against new (found of 76; wrong `[stated]`): f77 39 to 33, 12 to 14; f78 37 to 31, 7 to
    10; F88 base 31 and 32 to 29 and 29, 4 and 4 to 3 and 5; F88 new 30 and 30 to 26 and
    24, 2 and 2 to 4 and 5. Under the new scorer F88 does not meet its own rule; T46
    follows it up. A first judge without `broader` and with the names check deciding
    failed the hand read: it called most statements holding a true extra detail wrong.
  - **Baseline, 2026-10-14** (main `fcc8285`, turns pipeline, GLM-5.3-Flash, digest
    prompts `56d06506906a135f`, judge prompt `79b5313192c2f4a1`; variants
    `main-2026-10-14-1` and `-2`): found 25 and 27 of 76, held-out 14 and 15 of 41, dev 11
    and 12 of 35; wrong `[stated]` 4 of 28 and 2 of 26. A prompt change is compared with
    these runs on held-out.
  - **Sandbox:** every eval run that calls a model uses a temporary `AFT_HOME` and a neutral
    working directory, so the F87 recall hook and capture do not reach the user's memory.
  - **Not in this change:** an `[observed]` recall key; paired per-session differences with
    bootstrap intervals (ROADMAP prompt idea 7); the F69 LongMemEval run, which follows this.
  - **Tests** (`tests/test_eval_digest.py`, with a fake judge): one run fact cannot answer two
    key items; a right quote under a wrong statement scores wrong; a run statement naming
    something neither key text holds is wrong with no judge call; a cached verdict makes no
    call; an `[observed]` fact is judged against its own quote; `replaced-rule` finds
    `settle` and does not count the superseded `yes` value from shared words; both splits are
    reported; the report holds the hashes, model, temperature and prompt shape. Review
    checkpoint: yes (the meter every later prompt change is judged by; lead reviews in full).
- **M19 Remaining test gaps** (ruling 2026-10-13; ROADMAP finding M19).
  - **Root:** the two tests in `tests/test_evidence.py` that rely on the OS refusing a mode
    (the archive at `0o000` and at `0o444`) skip when `os.geteuid() == 0`, with the reason
    `test_install_pi.py` already gives, since root reads and writes through any mode. No
    other test depends on a refused mode.
  - **Timing:** `pyproject.toml` registers a `timing` marker, and every test that asserts a
    wall-clock budget carries it: the 150 ms block build in `tests/test_inject.py`, the hook
    and record budgets in `tests/test_queue.py`, and any in `tests/test_adapters.py` or the
    recall tests. A default run still includes them (DESIGN 10's budgets are tested on the
    user's host); `pytest -m "not timing"` leaves them out on a loaded host, and the
    README's section that runs `uv run pytest` says so.
  - **Findings named in their tests:** every finding PR from the review of commit `36be494`
    (#136 C1, #137 H1 H2 M1, #138 H3 H4 M3, #139 H6 M7, #140 H7, #141 H5, #143 H8, #147 M12)
    added tests. The builder reads each PR's description, finds the test that covers each
    finding it names, and names the finding in that test's docstring, as the H3, H4 and M12
    tests already do. A finding with no covering test gets one. The PR lists each finding
    with its test.
  - Review checkpoint: no.
- **T46 What F88 loses under the corrected scorer** (ruling 2026-10-14; follows M16 and
  F88). Under M16's scorer, F88's prompt change finds fewer key items than the prompts
  before it (held-out 17 and 17 against 14 and 14; all 76: 29 and 29 against 26 and 24)
  and wrong `[stated]` facts do not fall (3 and 5 against 4 and 5). Two runs each cannot
  tell a loss of 3 items in 41 from run-to-run noise. F88 stays merged meanwhile: it keeps
  one-time work orders out of memory and out of the notes waiting on the user.
  - **Lead, measure:** two more runs of each side (the commit before F88 and main) through
    `scripts/eval_digest.py run`, sandboxed, then `score-turns`, so each side has four
    runs on the same key and split. Report found and wrong `[stated]` per run, on held-out
    and on all items.
  - **Lead, read:** every held-out key item that two or more pre-F88 runs find and two or
    more F88 runs miss, with F88's statement for that turn if any, and name which part of
    the F88 wording (the request rule in `turns.md`, the never-a-fact sentence in
    `digest.md`) dropped it.
  - **Ted decides:** keep F88 as is (the difference is noise), narrow its wording (a
    following task with its own merge rule on held-out), or revert it.
  - **Measured, 2026-10-14** (pre-F88 `a19d85c` runs `f88-base-1` to `-4`; F88 runs
    `f88-new-1`, `-2` and main `main-2026-10-14-1`, `-2`): found of 76, pre-F88 29, 29,
    34, 33 against F88 26, 24, 25, 27; held-out of 41, 17, 17, 18, 15 against 14, 14, 14,
    15; wrong `[stated]` 3, 5, 15, 10 against 4, 5, 4, 2. Every pre-F88 run finds more
    than every F88 run, and F88 halves the wrong `[stated]` facts. Eight key items are
    found by at least two pre-F88 runs and at most one F88 run (two of them held-out). Six
    are one-time work orders the key labels durable ("Apply the PASEO.md edits.", "Add
    --print-timeout 60m to the lead prompt's agy command.", "Apply all the proposed
    OpenViking integration changes except the tmux change."), which F88's ruling keeps
    out of memory. Two hold a lasting value inside an order: "Use /home/ted/src/switchboard
    as the project location." and the voxtype sound theme set to default. F88 gains two
    items the pre-F88 runs miss.
  - **Ruling 2026-10-14:** F88 stays. The key follows F88's ruling: the 16 one-time orders
    it labelled durable move to a `request` list on their turns (backup
    `key-turns.2026-10-14-pre-t46.json`), leaving 60 durable items (33 held-out). On that
    key, pre-F88 found 22, 23, 27, 26 and F88 23, 20, 21, 23 (held-out 13, 13, 15, 12
    against 12, 11, 11, 12), with wrong `[stated]` 10, 11, 22, 17 against 7, 9, 8, 6. T47
    takes the two lasting values.
- **T47 An order keeps the lasting value it sets** (ruling 2026-10-14; follows T46).
  F88 keeps a one-time order out of memory, and with it a value the order sets that a
  later session needs: "use /home/ted/src/switchboard" (where the project lives), "change
  to default" (the voxtype sound theme). The order is not memory; the value is.
  - **`turns.md`:** a request that sets a lasting value (a location, a name, a setting, a
    choice of tool) also gives that value as its own item, labelled decision or fact, with
    a statement that says the value and not the order ("The switchboard project lives at
    /home/ted/src/switchboard."). One neutral, invented example.
  - **`digest.md`, `decide.md` and `review.md`:** the never-a-fact sentence on one-time
    orders adds that a lasting value an order sets is not the order, and is kept as a fact
    or decision stating the value.
  - **Measure, before merging (decision rule set now):** four runs of the change through
    `scripts/eval_digest.py run` (sandboxed), scored with `score-turns` on the 60-item key,
    against main's four (`f88-new-1`, `-2`, `main-2026-10-14-1`, `-2`): the held-out mean
    found rises by at least 1 of 33; the switchboard location and the voxtype sound theme
    are each found in at least two of the four runs; and the mean wrong `[stated]` count
    (an order kept as a fact counts wrong) does not rise by more than 1. Merge only if all
    three hold.
  - **Tests:** prompt-shape tests updated for the four prompts. Review checkpoint: yes
    (prompt change; lead reviews in full).
  - **Measured, ruling 2026-10-14: not merged** (#185 closed). As built (runs `t47-1` to
    `-4`): held-out 13, 15, 15, 15 against main's 12, 11, 11, 12; the switchboard location
    found in 4 runs and the sound theme in 3; wrong `[stated]` 11, 10, 10, 14 against 7, 9,
    8, 6 (+3.75). A second wording that names the values a later session must know and
    excludes work started or finished (`t47b-1` to `-4`): held-out 12, 13, 13, 13, wrong
    `[stated]` 15, 12, 13, 13 (+5.75). Both fail the third condition. About half the new
    wrong lines are true values the key does not hold (the sound theme now default, where
    firstmate's copies live); the rest are requests turned into status ("the user has
    started the run"). Asking the turns call to produce a second item from a request makes
    it label more requests as facts. The two lasting values stay unrecovered.
- **T48 Low findings: consistency and portability** (ruling 2026-10-14; ROADMAP Low
  findings L2, L7, L9, L11). Each item is checked against main first; one already fixed is
  named in the PR and left alone.
  - **L2:** `docs/configuration.md` names the default `digest.pipeline` as `config.py` sets
    it (`turns`), and the summary schema and the docs agree on the summary's fields.
  - **L7:** `aft inject` and the session-start hook resolve a directory's project with the
    same `never=capture.exclude_paths`, so the two cannot disagree; a test runs both on an
    excluded path.
  - **L9:** the `git merge-base` call in `dream/maintain.py` passes `--end-of-options`
    before revisions, and the `rg -F` lookup is replaced by Python's `in` on the text
    already read (no subprocess); behaviour unchanged, tests show both.
  - **L11:** digest metadata is keyed so two facts with the same statement do not collide;
    the duplicated line assignment in `dream/schedule.py` is removed; `eval/longmemeval.py`
    reports peak RSS in MB on Linux and macOS (`ru_maxrss` is KB on Linux, bytes on macOS);
    a request with `json_schema` sets `strict: true` where the endpoint takes it, with a
    test on the request body.
  - Review checkpoint: no.
- **T49 Low findings: one copy of each check** (ruling 2026-10-14; ROADMAP Low findings
  L3, L12). Behaviour-preserving: the full suite passes unchanged except tests that name a
  removed helper.
  - **L3:** `explicit_correction` is never set by any writer, so R3's correction bypass
    cannot run. It is removed from `dream/rules.py` and `dream/apply.py` with its tests;
    DESIGN keeps no mention of it (the lead checks DESIGN 9).
  - **L12:** fenced-code detection, digest file-name parsing and the `[stated]` trust
    checks each live once. The `[stated]` checks in `digest.verify` and `notes.settle` (or
    `apply.settle`) call one function; fence detection and digest-name parsing move to one
    module each and every caller uses it.
  - Review checkpoint: yes (R1, R2 and R3 code paths; lead reviews in full).
- **T50 A project merge does not copy the source's listing** (ruling 2026-10-14; found by
  `aft lint` on the user's vault). `aft project merge` moves every file of the source
  project directory into the destination, and a name the destination already holds is
  kept with a number appended (F70). The source's `index.md`, a listing afterthought
  writes, therefore lands as `index-2.md`: a page with no frontmatter that `aft lint`
  reports, listed in the destination's `index.md`.
  - **Merge:** the reserved listing files (`lint.RESERVED`: `index.md`, `log.md`) of the
    source are not moved; the destination's listing is rewritten to name the pages it now
    holds, the way afterthought writes listings elsewhere.
  - **Lint:** a file named `index-<n>.md` or `log-<n>.md` beside a listing is reported
    with what to do ("a listing left by a project merge; delete it and its line in
    index.md"), so a vault merged before this change is told how to repair itself. Agents
    do not edit vault pages; the user deletes it.
  - **Tests:** merging two projects that each have `index.md` leaves one listing naming
    every page and no `index-2.md`; lint names a leftover `index-2.md` with the repair.
  - Review checkpoint: no.
- **T51 Hot spots walk once per run** (ruling 2026-10-14; ROADMAP Low finding L10).
  Behaviour-preserving: each answer stays the same, only how often the disk is read
  changes. Each item is checked against main first.
  - **`dream/apply.py`:** the digest walk done for each lookup (worker-digest names, a
    digest's record) is done once per project run and looked up from what it built.
  - **Budget:** a budget check does not parse the whole `usage.jsonl` each time; the day's
    total is read from the file's tail, or kept beside it and rebuilt from the file when
    the two disagree (the file stays the truth).
  - **`reconcile.py`:** the newest session is not re-read in full when only its new
    entries are needed.
  - **`index.py`:** resolving links walks the vault once per index build, not once per link.
  - **Tests:** each path counts its reads (a monkeypatched walk or open) on a fixture with
    many digests or links and shows one walk per run; the answers match the current code's
    on the same fixture. Review checkpoint: no.
- **F89 A fact refused for its names is restated once in its source's words** (ruling
  2026-10-14; ROADMAP prompt idea 1). R2's names check (`digest.ungrounded`) is the largest
  cause of lost facts: over main's four runs on the eval set, 64 `[stated]`, 108
  `[observed]` and 155 `[inferred]` facts were refused because their statement names a
  word the source does not hold. Most are the model's own spelling of a name the user
  wrote otherwise ("Groq" for the user's "grok ai", "PLT Focus" for "PL Focus") or a term
  it added ("Bluetooth"). The fact was there; only its wording broke the rule.
  - **Repair call:** after a chunk's facts are verified (both pipelines), the facts refused
    only by the names check go to one more call, at most one per chunk: for each, the
    refused statement, the words it names that the source lacks, and the text it must be
    grounded in (the user's turn and the proposal's words for `[stated]`, the cited
    entry's text for the rest, each cut to what the check reads). The model answers, per
    fact, a restated sentence that writes every name, number and key term as that text
    writes it, or nothing when the claim cannot be said without the missing words. A new
    prompt `restate.md` and schema `restate.schema.json`, schema-constrained (R9).
  - **Every check runs again:** a restated fact goes through `verify` exactly as a first
    answer does (quote found, own words, question, R2 names); a restated fact that fails
    is dropped with its new reason. The trust rules do not change; the quote, tag, kind,
    term and entry stay as the first answer gave them, and only the statement is new.
  - **Setting:** `digest.restate` (default `true`), documented in
    `docs/configuration.md`; the call counts against the day's budget like any digest call
    and is skipped when the budget is spent.
  - **Measure, before merging (decision rule set now):** four runs through
    `scripts/eval_digest.py run` (sandboxed) scored with `score-turns` on the 60-item key,
    against main's four: held-out mean found rises by at least 1 of 33; mean wrong
    `[stated]` does not rise by more than 1; observed facts judged unsupported do not rise
    by more than 2 per run on average. Tokens per run are reported.
  - **Tests:** a fact refused for a name the source spells otherwise is restated and kept
    with the source's spelling; a restated fact that still names an unheld word is
    dropped; a restated fact whose quote is a paste or a question is dropped; nothing is
    sent when no fact was refused for names; a spent budget skips the call; the setting
    off makes no call. Review checkpoint: yes (a new path to a kept fact; lead reviews in
    full).
  - **Measured, ruling 2026-10-14: not merged** (#190 closed). Four runs (`f89-1` to `-4`,
    restate examples that keep the claim) against main's four: held-out found 14, 15, 14,
    14 against 12, 11, 11, 12 (+2.75); wrong `[stated]` 10, 8, 12, 21 against 7, 9, 8, 6
    (+5.25); observed judged unsupported 9, 10, 7, 14 against 8, 4, 6, 9 (+3.25); tokens
    about 1% more. The restated facts are partly true facts the key does not hold ("when
    the user pushes the caps key they only hear a beep") and partly what the names check
    had been refusing for a second reason: one-time orders ("Apply all of the proposed
    firstmate changes except the tmux change."), session status ("Ted has restarted the
    session."), troubleshooting notes. R2's names check also filters what the keep-test
    should, so a recall gain there needs the keep-test to hold on its own first.
  - **The F89 measurement does not stand.** The restate call checks the day's budget,
    and the sandbox home the runs shared had spent its default 200,000 tokens hours
    before, so the call almost never ran: the F89 runs made 146 to 148 calls against
    main's 143 to 144. The difference above is therefore what four runs of nearly the
    same code vary by: about 3 held-out items and 5 wrong `[stated]` facts. F91 measures
    restating again, behind F90, in a sandbox whose budget does not run out, on the grown
    key (T52, T53). T47's +3.75 wrong `[stated]` is inside the same spread; T47 makes no
    budgeted call, so its runs stand, but its result is inconclusive rather than a loss.
- **T52 The key rules on what the runs call wrong** (ruling 2026-10-14; lead and Ted).
  `score-turns` counts every `[stated]` fact the key does not hold as wrong, so a true,
  durable fact the key never labelled reads as an error (T47, F89). The lead collects every
  distinct `[stated]` statement the runs of 2026-10-14 (`main-*`, `f88-*`, `t47*`, `f89-*`)
  counted wrong, rules each one `durable` (it passes the keep-test: a future session would
  act differently for knowing it), `order`, `status` or `noise`, and adds the durable ones
  to `key-turns.json` on the turn their quote comes from, with the quote. Rulings the lead
  is unsure of go to Ted. The split is unchanged (it is by turn).
- **T53 The key grows by twenty sessions** (ruling 2026-10-14; lead and Ted). 60 durable
  items with 33 held-out cannot show a change of one or two items against a run-to-run
  spread of three to four. The lead exports about twenty more real sessions read-only to
  `~/src/aft-eval-data` (registered projects, primary sessions, a spread of projects and
  lengths, none a worker's), writes their turns files, and labels them three ways as F54
  did: the lead, Gemini (`label_turns.py gemini`) and the local model. The lead settles
  disagreements and sends Ted only the ones it cannot settle. New turns join the split by
  the same hash rule without moving any existing turn. Goal: about 150 durable items.
  Main's baseline is re-run on the grown key before any task below is measured.
  - **Done 2026-10-14:** 23 sessions exported (373 user turns: LibreCoach site and
    releases, backups, retirement planning, inference, pi, and two long Afterthought lead
    sessions), labelled by the local model and Gemini with the labelling prompt following
    F88, and settled by the lead: 30 durable items kept (decisions and tax facts of the
    retirement plan, backup setup, release preferences, profile facts, standing
    permissions); one-time orders, session status, troubleshooting, settle replies, and
    choices the repository records (site styling, config values) left out. The key holds
    97 durable items over 639 turns (50 held-out, 47 dev); no existing turn changed side.
  - **Measuring on it:** every run's sandbox home sets `work.daily_token_budget` far above
    a day of runs, since a budgeted call skips itself once the budget is spent (the F89
    runs lost their restate call that way). Four runs of each side, as before; a merge
    rule on the grown key states its threshold against the spread of main's own four runs.
- **F90 A kept fact must pass the keep-test on its own** (ruling 2026-10-14). Extraction
  and the keep-test share one call, so a call asked to find more also keeps more that is
  not worth keeping (T47, F89): orders, session status, troubleshooting steps. A second,
  narrow call asks only the keep-test.
  - **Keep call:** with `digest.keep_check` on, every fact a turns digest keeps (any
    source) is asked once, in batches of up to 20 per call: would a future session act
    differently, or ask the user again, without it; or is it a one-time order to do work,
    the status of work in the session (started, ran, restarted, finished, failed), a step
    of troubleshooting that ended with the session, or a value the repository already
    records? The schema gives per fact `reason` first, then `keep` (true or false). A fact
    answered false is dropped with the reason recorded on the page's dropped list. A new
    prompt `keep.md` and schema `keep.schema.json`; the never-a-fact list in `digest.md`
    is the source of its wording, so the two say the same thing.
  - **Trust:** the call only removes facts; it never adds, edits or retags one.
  - **Setting:** `digest.keep_check`, default `false` until measured; the calls count
    against the day's budget and are skipped when it is spent (the facts are then kept as
    today).
  - **Measure, before merging (decision rule set now), on the grown key (T52, T53):** four
    runs with the check on against main's four re-run on the same key: mean wrong
    `[stated]` falls by at least 2; held-out mean found does not fall by more than 1;
    observed judged unsupported does not rise. If all three hold, the default becomes
    `true`. Tokens per run are reported.
  - **Tests:** a fact answered false is dropped with its reason; a true one is kept as it
    was; the setting off makes no call; a spent budget makes no call and keeps the facts;
    a batch of 25 facts makes two calls. Review checkpoint: yes (a model call that drops
    facts; lead reviews in full).
  - **Measured, ruling 2026-10-15: merged, on by default.** On the grown key, four runs
    each against main's four (`g-main-1` to `-4`: held-out 16, 17, 17, 17 of 50; wrong
    `[stated]` 32, 36, 29, 28). The first keep prompt cut wrong `[stated]` to 11.5 but
    held-out found to 11 (it dropped places, names and people a session could look up).
    The prompt as merged keeps what a session could rediscover (where something lives,
    what it is called, who someone is, the user's setup) and keeps when unsure
    (`g-f90b-1` to `-4`): held-out 19, 18, 19, 15 (mean 17.75); wrong `[stated]` 11, 16,
    20, 11 (mean 14.5, from 31.25); observed judged unsupported 17.5 from 23.75; tokens
    +12%. Verdicts are matched to facts by their item label, and a keep call cut for
    length keeps its batch.
- **F91 F89 again, behind the keep call** (ruling 2026-10-14). After F90 merges, the F89
  branch (`f89-restate`, restate examples that keep the claim) is rebased onto main with
  the restated facts passing F90's keep call like any other, and measured on the grown key
  with F89's rule against main with F90 on.
  - **Measured, ruling 2026-10-15: not merged** (branch `f91-restate-behind-keep`). Four
    runs (`g-f91-1` to `-4`, each in its own sandbox home with a budget that does not run
    out; the restate call ran: 432 to 441 calls a run against 357 to 366) against main
    with F90 (`g-f90b-1` to `-4`): held-out found 20, 22, 18, 18 against 19, 18, 19, 15
    (+1.75, passes); all items 40 against 34.5; wrong `[stated]` 19, 13, 22, 18 against
    11, 16, 20, 11 (+3.5, fails the +1 limit); observed judged unsupported 20, 23, 37, 21
    against 13, 21, 17, 19 (+7.75, fails the +2 limit); tokens +11%. Restating finds more,
    but restated `[observed]` facts are often unsupported by their quote, and the keep
    check does not judge support. A version that restates only `[stated]` facts is the
    next thing to try.
- **F92 Restate only `[stated]` facts** (ruling 2026-10-15; follows F91). F91 with one
  change: only a `[stated]` fact refused for its names is restated, in the user's words
  (or the proposal's, for an approval). Restated `[observed]` and `[inferred]` facts were
  the ones its quote did not support. Branch `f92-restate-stated`. Measured with F89's
  rule against main with F90 (`g-f90b-1` to `-4`), four runs each in its own sandbox home:
  held-out found rises by at least 1 of 50; mean wrong `[stated]` rises by no more than 1;
  observed judged unsupported rises by no more than 2. Tests: an observed fact refused for
  its names goes to no restate call.
  - **Measured, ruling 2026-10-15: not merged** (branch `f92-restate-stated`). Four runs
    (`g-f92-1` to `-4`; the first attempt was void, the model endpoint was down and every
    call failed) against `g-f90b-1` to `-4`: held-out found 21, 19, 21, 21 (mean 20.5,
    +2.75, passes); all items 40.5 against 34.5; observed judged unsupported 14.5 against
    17.5 (passes); wrong `[stated]` 16, 24, 20, 21 (mean 20.25, +5.75, fails the +1
    limit); tokens +3%. The new wrong lines are mostly one-time orders and approvals the
    keep check lets through once restated ("Spec recall on prompt and start L8 on pi.",
    "F85 is approved for merge and the search token budget should be specced as a
    follow-up task."), session pacing ("pi is available again and may be used if
    needed."), and statements naming the user ("Ted has disabled herems."). The keep check
    has to refuse approvals of one-time work, and names, before restating can pay.
- **T54 A call that runs out of output is asked again on half its input** (ruling
  2026-10-14). An `observed` call on a long chunk sometimes writes until `max_tokens` and
  returns broken JSON (main's runs: about one and a half chunks per run, each losing every
  observed fact of its chunk). When a digest call's answer fails its schema and the
  endpoint reported `finish_reason: length`, the chunk's entries are split in two halves
  and each half is asked once, in place of the ordinary retry; a half that fails again is
  refused as today. Tests: a length-cut answer is asked again as two halves and both
  halves' facts are kept; an answer that fails for another reason keeps today's retry; a
  half that fails again is refused. Review checkpoint: no.
- **F93 The keep check refuses approvals of one-time work** (ruling 2026-10-15; follows
  F90 and F92). Restated facts in F92 passed the keep check as approvals ("F85 is
  approved for merge and the search token budget should be specced as a follow-up
  task."), as the pace of work ("pi is available again and may be used if needed."), and
  as statements naming the user. `keep.md` says an approval of one-time work is the order
  it approves (a lasting permission or a design or setting choice the approval settles
  stays kept), that the pace of work and what is available for it are not facts, and that
  a statement calling the user by a personal name is not kept; two examples join it.
  Branch `f93-keep-approvals`.
  - **Measure, before merging (decision rule set now):** four runs against main with F90
    (`g-f90b-1` to `-4`), each in its own sandbox home: mean wrong `[stated]` falls by at
    least 2; held-out mean found does not fall by more than 1; observed judged unsupported
    does not rise by more than 2. If it merges, F92 is measured again on top of it.
  - **Tests:** the prompt-shape test for the new sentences. Review checkpoint: yes (a
    prompt that drops facts; lead reviews in full).
  - **Measured, ruling 2026-10-15: not merged** (branch `f93-keep-approvals`). Four runs
    (`g-f93-1` to `-4`) against `g-f90b-1` to `-4`: wrong `[stated]` 14, 10, 14, 11 (mean
    12.25, -2.25, passes); held-out found 11, 15, 17, 17 (mean 15, -2.75, fails the -1
    limit); observed judged unsupported 17.25 against 17.5. A tighter keep prompt trades
    recall for precision again, and the trades now sit near what four runs of one prompt
    vary by (held-out 15 to 19 in main's own four).
- **F94 Restating behind the tighter keep check** (ruling 2026-10-15; F92 and F93 together).
  F92 found more but kept more wrong `[stated]` lines; F93 refused more wrong lines but
  cost recall. Their differences sit near what four runs of one prompt vary by, so this
  comparison uses eight runs a side. Branch `f94-restate-and-keep` (F93's keep prompt with
  F92's `[stated]`-only restate).
  - **Measure, before merging (decision rule set now):** eight runs of F94 (`g-f94-1` to
    `-8`) against eight of main with F90 (`g-f90b-1` to `-4` and `g-f90c-1` to `-4`), each
    run in its own sandbox home with a budget that does not run out, at most six at once:
    held-out mean found rises by at least 1 of 50; mean wrong `[stated]` rises by no more
    than 1; observed judged unsupported rises by no more than 2. The spread of each side's
    eight runs is reported beside the means.
  - **Measured, ruling 2026-10-16: passes all three parts** (branch
    `f94-restate-and-keep`). Eight runs a side, all sixteen scored with the same key and
    judge:
    - Held-out found: F94 20, 21, 20, 14, 21, 21, 22, 17 (mean 19.5, range 14 to 22) against
      main 19, 18, 19, 15, 15, 16, 15, 15 (mean 16.5, range 15 to 19): +3.0, passes.
    - Wrong `[stated]`: F94 13, 14, 11, 14, 10, 9, 6, 13 (mean 11.25, range 6 to 14) against
      main 11, 16, 20, 11, 10, 16, 14, 10 (mean 13.5, range 10 to 20): -2.25, passes.
    - Observed judged unsupported: F94 mean 15.125 (range 9 to 23) against main 16.25
      (range 13 to 21): -1.125, passes.
    - All found: F94 mean 40.0 of 97 (range 32 to 44) against main 33.125 (range 28 to 36).
      `[stated]` lines kept: F94 mean 49.9 against main 44.9.
- **T56 One run fact can answer several key items** (ruling 2026-10-18; lead and Ted).
  `score-turns` pairs facts one to one (`match_facts`), so a run fact that holds two key
  items answers only one of them, and the other reads as missed: "Staging is still on
  db-staging-02.internal; only the read replica moved to db-staging-03.internal." pairs
  with one of its two items, and "The user's two major concerns are ..." with one of the
  two concerns. A fact whose quote holds the key item's quote can also fall short of both
  thresholds: the key quote "I'm a FL resident, so no state taxes." stands inside the run's
  quote, which begins "my primary concern is cost reduction and simplicity.", at a quote
  ratio of 0.56 and a statement ratio of 0.38. In the eight F94 runs, 29 missed item-runs
  have a kept fact that overlaps them and no pair, and 24 more are paired and judged
  `different`: about 3 to 4 items a run, the size of F94's whole gain and of the spread
  between runs.
  - **Pairing:** in `score-turns` only, a key item pairs with a run fact when their quotes
    overlap or their statements agree as `fact_overlap` says today, or when the key
    item's quote, folded as `quote_tokens` folds it, stands whole inside the run fact's
    folded quote. A run fact may answer several key items; each pair is judged on its
    own, and a key item still takes at most one run fact, the strongest. A run fact counts
    as wrong `[stated]` only when no key item pairs with it or every pair is judged
    `different`. `compare`, which matches backends against each other, keeps one-to-one
    pairing.
  - **Re-score:** the sixteen runs on disk (`g-f94-1` to `-8`, and main's `g-f90b-1` to
    `-4` and `g-f90c-1` to `-4`) are scored again with the same key and judge, and the new
    means and spreads go beside the old ones here. They become the baseline every later
    recall task is measured against.
  - **Tests:** a run fact holding two key items' quotes answers both; a key item whose
    quote stands inside a longer run quote pairs; a key item takes only its strongest run
    fact; a run fact paired only with `different` verdicts counts as wrong `[stated]`;
    `compare` still pairs one to one. Review checkpoint: no.
  - **Built 2026-10-18** (#196, with a lead fix: a wrong `[stated]` fact counts in the
    split of the first key item it pairs with). The sixteen runs re-scored on the same key
    (97 items, 50 held-out):
    - F94: held-out found 23, 24, 24, 19, 24, 24, 26, 21 (mean 23.1, was 19.5); all found
      mean 46.25 (was 40.0); wrong `[stated]` mean 10.5 (was 11.25).
    - main with F90: held-out found 20, 22, 22, 18, 17, 17, 18, 18 (mean 19.0, was 16.5);
      all found mean 38.0 (was 33.1); wrong `[stated]` mean 13.0 (was 13.5).
- **T57 The key rules on what every run misses** (ruling 2026-10-18; lead and Ted; after
  T56). The T52 pass for misses: 38 key items are missed by all eight F94 runs, and some
  are what the keep-test is meant to refuse or what the pipeline rightly replaces. Among
  them: one-time orders ("Create the WirePlumber config that sets bluez5.headset-roles to
  hands-free only, removing HSP.", "Accept the proposed review feedback changes, ..."),
  the status of work ("The bad timedatectl command with --adjust-system-clock already
  ran.", "The user put the proposed multi-port extension change on hold for now."), a
  passing state ("The user's Groq API key is brand new."), and items a later turn of the
  same session replaces ("The staging database moved to db-staging-03.internal last
  week.", "Every release goes out to the canary before the fleet, with no exceptions.",
  "Use agent-switchboard as the name in the prompt and create ~/agent-switchboard.").
  - **Ruling:** after T56's re-score, the lead lists every key item missed by all eight
    F94 runs and rules each one `durable` (it passes the keep-test and nothing later in
    its session replaces it), `order`, `status`, `passing` or `replaced`. Items ruled
    anything but `durable` leave `key-turns.json`; a `replaced` item's turn keeps the item
    that replaces it. Rulings the lead is unsure of go to Ted. The previous key is kept
    beside it (`key-turns.2026-10-18-pre-t57.json`), and the split does not change.
  - **Re-score:** the sixteen runs are scored again on the ruled key, and the means and
    spreads go here as the baseline for F95 and later tasks.
  - **Ruled 2026-10-18:** after T56, 32 items are missed by all eight F94 runs. Ten leave
    the key (eleven lines, since one turn appears in two exported copies of its session):
    `order` for the choice of option 1 among the punctuation fixes and both WirePlumber
    config approvals; `status` for the timedatectl command that already ran, the
    crew-dispatch path that did not exist, and the multi-port change put on hold;
    `passing` for the Groq key's age; `replaced` for the agent-switchboard name, the
    db-staging-03 move and the canary rule. Three go to Ted and stay in the key until he
    rules: "Accept the proposed review feedback changes, including posting to ted when a
    member goes BLOCKED." (a lasting design choice, or one-time work the repository
    records?), "The user has disabled Hermes." (the user's setup, or a troubleshooting
    step?), "Disable the sound voxtype plays when the transcribed words print." (an order,
    or a lasting setting?). The other 19 are `durable`. The rulings are in
    `~/src/aft-eval-data/t57-rulings.json`. The key holds 86 items over 639 turns (43
    held-out, 43 dev).
  - **Baseline on the ruled key** (T56's scorer):
    - F94: held-out found mean 23.1 (range 19 to 26) of 43; wrong `[stated]` mean 10.5
      (range 6 to 14); observed judged unsupported mean 13.6; all found mean 46.25 of 86.
    - main with F90: held-out found mean 18.4 (range 16 to 22); wrong `[stated]` mean
      13.6 (range 9 to 20); observed judged unsupported mean 15.0; all found mean 37.4.
    - Observed judged unsupported moved by up to 1.5 on a re-score with no change to the
      runs or to what it judges, so judge calls outside the cache vary by about that much.
- **F95 A quote that ends its own sentence is judged by that sentence** (ruling
  2026-10-18; Ted; touches R2's quote checks). `enclosing_sentence` in `digest.py` scans
  forward for the sentence's end from the character after the quote. A quote that ends
  with its sentence's own `.`, `!` or `?` has that mark behind the scan's start, so the
  slice runs on into the next sentence. A user's statement followed by a question is then
  read as a question: "I use Vorta backup and havve a 3 hour schedule." is refused as
  `the quote is a question, never a settled item` because the next sentence asks "would an
  hourly schedule cost much diskspace and activty?". The same slice makes a short quote
  that is a whole sentence ("usually about 3-4.") fail the fragment check, since it no
  longer equals its enclosing sentence. The code then does not do what its docstring says
  (the sentence of the text that holds the quote); R2 itself does not change.
  - **Fix:** when the quote's last character is `.`, `!` or `?` and the text after it is
    whitespace or the end of the text, the sentence ends at the quote's end. Both callers
    take the fix: `stated_refusal` (digest verification and `dream/apply.py`) and the
    fragment check in `digest.py`. A quote cut from inside a sentence that asks is still
    refused: "I'm going to stikc with C4, does the compose file need to change at all or
    is it ready?" stays a question.
  - **Replay, before measuring:** the 44 `[stated]` facts the eight F94 runs dropped as a
    question, checked again with the fix against their turns' text: 32 are no longer
    refused (among them the key items "The user uses Vorta backup with a 3 hour schedule
    and is more worried about their documents.", "The Actual password should be replaced
    via agent vault.", "Last time a librecoach ha-addons-alpha was used for the testing.",
    "The user usually runs about 3-4 agents."), and 12 still are (all six "The user is
    going to stick with C4." among them). The builder reproduces the count with the code
    as built and reports it on the PR.
  - **Measure, before merging (decision rule set now):** eight runs (`g-f95-1` to `-8`),
    each in its own sandbox home with a budget that does not run out, at most six at once,
    against the eight F94 runs (`g-f94-1` to `-8`), all scored on T57's key with T56's
    scorer: held-out mean found does not fall; mean wrong `[stated]` rises by no more than
    1; observed judged unsupported rises by no more than 2. The spread of each side's
    eight runs is reported beside the means.
  - **Tests:** a quote ending with its own full stop and followed by a question is not
    refused, in digest verification and in `dream/apply.py`; a whole-sentence short quote
    with its full stop passes the fragment check; a quote cut from a sentence that ends in
    a question is refused; a quote holding a question mark is refused; a quote spanning
    lines still yields its sentence. Review checkpoint: yes (R2 quote checks; lead reviews
    in full).
  - **Measured, ruling 2026-10-18: not merged** (#195 closed, branch
    `f95-quote-ends-sentence`). The lead's full review found the change as specified, and
    the replay gave 32 of 44 no longer refused and 12 still refused, as above. Eight runs
    (`g-f95-1` to `-8`) against the eight F94 runs, all on T57's key with T56's scorer:
    - Held-out found: 24, 22, 25, 23, 24, 28, 22, 24 (mean 24.0, range 22 to 28) against
      F94's mean 23.1 (range 19 to 26): +0.9, passes.
    - Wrong `[stated]`: 11, 18, 7, 13, 9, 13, 13, 18 (mean 12.75, range 7 to 18) against
      F94's 10.5 (range 6 to 14): +2.25, fails the +1 limit.
    - Observed judged unsupported: mean 11.4 against 13.6, passes. All found: mean 47.25
      of 86 against 46.25.
    - Of the 18 more wrong lines over eight runs, 8 are facts whose quotes F94 refused as a
      question. They are wrong for how they are written more than for what they claim:
      they call the user by name ("Ted uses Vorta backup with a 3 hour schedule.", kept
      beside "The user uses Vorta backup with a 3 hour schedule."), or name another person
      the key does not hold ("Chuck has installed the latest prod."). The other 10 lie in
      what eight runs of one pipeline vary by (the rerank pool, a captain.md guardrail).
      The keep check's sentence against naming the user does not hold: 55 of the 2,432
      facts the eight F94 runs kept call the user by name or by a guessed pronoun. F95
      can be measured again once statements stop naming the user.

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
