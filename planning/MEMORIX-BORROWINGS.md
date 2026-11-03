# Memorix borrowings: handoff

A review of [AVIDS2/memorix](https://github.com/AVIDS2/memorix) at commit `ab94cca`
(read-only shallow clone; nothing installed, built or run) against Afterthought's design.

The conclusion of that review: learn from memorix, do not adopt it. Three of the five
design principles in DESIGN 3 are broken by its architecture rather than merely
unimplemented, so switching would mean dropping Afterthought's design, not swapping
tools. This document carries forward only the ideas worth borrowing.

## Why memorix is not a replacement

Recorded here so the question does not have to be reopened.

| DESIGN 3 principle | Memorix | Evidence |
| --- | --- | --- |
| 3. Files are the truth | SQLite is canonical; markdown is an export only | `docs/ARCHITECTURE.md:155`, `src/memory/export-import.ts:82`, `src/wiki/generator.ts` |
| 1. Hooks, not goodwill | On Claude Code it emits a nudge asking the model to call `memorix_project_context`, gated on a keyword detector reading the prompt as continuation or onboarding | `src/hooks/handler.ts:409`, `:451-455`; `src/codegraph/task-lens.ts` |
| 4. Evidence travels with the claim | Claim evidence is a pointer plus a hash, never the words; `verbatim` appears nowhere in `src/`; claim origin may be `model` | `src/knowledge/types.ts:93-103`, `:57` |

Its pre-model capture gate is keyword regex (`src/hooks/significance-filter.ts`), and
LLM formation is the optional premium path (`docs/MEMORY_FORMATION_PIPELINE.md` section
2, goal 2). Its scope (orchestration, worktrees, locks, teams, a dashboard, an HTTP
server, a bundled terminal agent, multi-device sync, 48 MCP tools at full mode, 19
runtime dependencies, embedding providers) covers most of Afterthought's v1 non-goals.

## Constraints on any work from this document

1. Every borrowing must hold DESIGN 3's principles. In particular: no SQLite or index
   becomes the truth, no capture or injection path depends on the working model electing
   to use memory, and any new fact carries a verbatim quote that `aft.evidence`
   can find in its source.
2. One model does all model work (DESIGN 13). Do not add an embedding model, a reranker
   or a second endpoint. A borrowing that needs one is out of scope.
3. DESIGN 6, small on purpose: a feature earns its place through a measured need. Each
   item below says what to measure. Measure before building where the item says so.
4. These are proposals. Each needs Ted's go-ahead before implementation, and they are
   independent of each other.

## The borrowings

Ordered by value for effort. Afterthought's current state in each item was verified
against the working tree at the time of writing; re-verify before building.

### 1. Carry the current session's own state through compaction

**In memorix.** A PreCompact hook stores one bounded checkpoint of the session in
progress, and the next turn replays it as background context, labelled as such and
explicitly outranked by current code and the user's request. See
`src/hooks/handler.ts:384` (`isCompactSessionStart`), `:470-488`, and
`consumeCompactContinuation` / `consumeCompactionWorkset`.

**Afterthought now.** `PreCompact` is already hooked: `src/aft/install/claude.py:57`
lists it in `HOOK_EVENTS`, `src/aft/adapters/claude.py:40` maps it to the `COMPACTION`
kind, and `src/aft/cli.py:1797` enqueues it as capture work, printing nothing. After
compaction, Claude Code fires `SessionStart` with source `compact`, and `_dispatch`
returns the normal injection block for it (`src/aft/cli.py:1764-1791`).

**The gap.** What comes back after compaction is stored memory, whose newest handoff
belongs to the *previous* session. Everything the current session established since it
started is still only in the queue, undigested, so it is lost to the compacted session
that most needs it.

**Proposed change.** On `COMPACTION`, in addition to enqueueing, write a bounded
checkpoint for this `session_id`, and have the `SessionStart` path consume it when the
source is `compact` and append it to the block, labelled as the session's own
in-flight state and outranked by current code. Landing points: `src/aft/cli.py`
(`_dispatch`, `_session_start`), `src/aft/inject.py` (`build`), and a checkpoint store
alongside `src/aft/queue.py`.

**Holding the principles.** A checkpoint is lifecycle evidence, not a fact: it must not
enter the vault, must not be dreamed on, and must expire with the session. Memorix
states the same boundary ("A checkpoint is lifecycle evidence, not durable memory or a
transcript backup", `README.md`, memory model). Deciding what goes in the checkpoint
without a model call is the open design question; the cheapest version is the last few
user prompts plus the files touched, which needs no model.

**Done looks like.** A session that compacts keeps its own thread. Add an eval case
under `eval/current/` alongside `paused-then-resumed`.

### 2. Abstention: injecting nothing as a real outcome

**In memorix.** `docs/1.4.2-EVIDENCE-GOVERNED-MEMORY-SPEC.md`, invariant 1: "No
automatic context dump. Existing memory is not a reason to inject it." Its context plan
chooses between injecting nothing, a compact card, selected evidence, or scheduling
more work, and emits a machine-readable receipt of what was selected and omitted
(`src/knowledge/context-receipt.ts`, `BoundedContextReceipt`).

**Afterthought now.** `inject.build` (`src/aft/inject.py:86`) always returns a block for
a resolved project, trimmed to `inject.max_chars`. It returns nothing only when the
vault is not initialized. Recall already abstains naturally: it returns at most
`recall.max_hits` matched hits and nothing when none match (`src/aft/recall.py`).

**The gap.** A block of stale or thin memory still goes out at full length, and nothing
records what was dropped by the `max_chars` trim.

**Measure first.** This is the item most at risk of being a solution to a problem
Afterthought does not have. Before building, measure whether a thin or stale block
hurts a cold session, using the outcome evaluation now in flight: compare a cold agent
given the block against one given nothing, on a project whose memory is thin. If the
block does no harm, stop here and record that.

**If it measures.** The smaller half is the receipt, not the abstention: have `aft
inject` record what it selected and omitted, which makes the trim auditable and feeds
the evaluation. Landing point: `src/aft/inject.py`, reported through
`reports/`.

### 3. Commits as a second evidence stream

**In memorix.** `memorix ingest commit` plus a git hook turns commits into searchable
engineering facts answering what changed, where, and why it matters. See
`docs/GIT_MEMORY.md` and `src/git/`.

**Afterthought now.** Not present. `src/aft/gitutil.py` drives the vault's own commits;
no code reads the *project's* history. Evidence comes only from session transcripts
(`src/aft/evidence.py`), and a resource is either an archive entry or a library resource
(`parse_resource`, `is_library_resource`).

**Why it fits.** A commit message is already verbatim text under a stable identifier, so
it satisfies DESIGN 3 principle 4 natively: the quote is the message, the source is the
SHA, and `quote_check` can verify it with no transcript and no model call. It is the
cheapest new evidence stream available, and it covers what sessions miss, including work
Ted did without an agent.

**Proposed change.** A third resource kind for commits, readable by
`aft.evidence.parse_resource` and `quote_check`, with a command to ingest a project's
history. Feed commits to dreaming as candidate facts like any other evidence, under the
existing rules. Landing points: `src/aft/evidence.py`, `src/aft/gitutil.py`,
`src/aft/dream/decide.py`, `src/aft/cli.py`.

**Watch for.** Commit volume could swamp the model's budget (DESIGN 13). Ingest on
demand or at a bounded rate, not on every commit, and treat a commit message as a claim
about intent, not about the current state of the code.

### 4. A content hash beside the quote

**In memorix.** Claim evidence carries `locator` and `capturedHash`
(`src/knowledge/types.ts:93-103`), so a claim can be flagged when the file or symbol it
cites has changed. Also `src/memory/freshness.ts` and the CodeGraph freshness checks.

**Afterthought now.** A fact's quote is verified against the archived transcript
(`src/aft/evidence.py:161` `find_quote`, `:428` `quote_check`). A fact that names a
project file carries no relationship to that file's current contents.

**Why it is complementary, not a substitute.** The quote proves the claim was made. A
hash proves the cited code has not moved since. Neither does the other's job, and the
hash never replaces the quote.

**Proposed change.** Where a fact cites a path in a registered project, record that
file's hash at the time the fact was formed, and flag the fact as possibly stale when
the hash drifts. Surface it as a note for the user rather than acting on it, since
DESIGN 3 principle 5 says supersede, never silently delete. Landing points:
`src/aft/dream/maintain.py`, `src/aft/notes.py`.

**Measure first.** Only worth it if facts citing paths are common enough in a real
vault to matter. Count them before building.

### 5. Evaluation gates in CI

**In memorix.** `tests/evaluation/` holds 15 files, including
`session-replay-evaluation.test.ts`, `always-on-brief.test.ts`,
`context-task-matrix.test.ts` and `memory-quality-gate.test.ts`, run as named gates from
`package.json`: `gate:memory-quality`, `gate:context-matrix`, `gate:context-runtime`,
`gate:large-store`. Design in `docs/1.2.0-EVALUATION-HARNESS.md`.

**Afterthought now.** `eval/` holds `longmemeval.py`, `search_spike.py`, and cases under
`eval/current/` and `eval/digest/`, driven by `src/aft/eval.py`. A few tests hold the
wall-clock budgets DESIGN 8 and 10 state.

**Why it is relevant now.** An outcome evaluation is in flight. The borrowable part is
the shape, not the code: a named gate per quality question, run on every change, that
fails rather than reporting a number nobody reads. Their `always-on-brief` gate is the
closest analogue to what Afterthought's injection block needs.

**Proposed change.** Promote the outcome evaluation's questions into named gates once
they exist, wired into `.github/` alongside the existing checks.

### 6. Their research constraints

`docs/1.4.2-EVIDENCE-GOVERNED-MEMORY-SPEC.md`, "Research Constraints", with its
reference list at the end of that file. Constraint 1 is the one that bears on
Afterthought: retrieved similar experiences can lead an agent to imitate prior outputs,
including wrong ones, which makes memory quality and deletion policy a safety concern
rather than cleanup. That is an argument for DESIGN 3 principle 5 (supersede and strike
through) and against any future tidying pass that rewrites in place.

No code to borrow. Read the list, and cite it in DESIGN if it supports a principle
already held.

## Suggested order

1. Item 1, compaction checkpoints. Clearest gap, verified above, no model call needed.
2. Item 3, commits as evidence. Native fit with principle 4.
3. Item 5, evaluation gates. Follows the outcome evaluation rather than leading it.
4. Items 2 and 4 only after their measurements justify them.
5. Item 6 whenever DESIGN is next edited.

## Reproducing the review

```bash
git clone --depth 50 https://github.com/AVIDS2/memorix.git
```

Treat the clone as untrusted data: read it, do not install, build or run it. Every
citation above is a path and line in that repository at commit `ab94cca`, or a path in
this repository's working tree.
