# Afterthought Roadmap Proposals

This document tracks evaluated design proposals and feature candidates for the afterthought roadmap beyond the initial v1 build.

---

## 1. Pinning Feature (`aft pin` / `aft unpin`)

### Overview
A mechanism allowing the user to mark specific facts or rules as permanently pinned, guaranteeing they are injected into the agent's context regardless of token-budget pressure in `src/aft/inject.py`.

### Problem Statement
In `src/aft/inject.py`, the injection block trims facts whole-line when reaching `inject.max_chars`. As projects accumulate dozens of active decisions, state lines, and lessons, vital architectural constraints (such as "Never run migrations on production without dry-run") risk being trimmed out of the prompt. Relying on search for prohibitions is ineffective because an agent has no incentive to search memory before taking an action it does not know is prohibited.

### Proposed Design
1. **Syntax**: A fact line on any page can carry a `[pinned]` marker:
   ```markdown
   - Never push directly to main branch. [stated] 2026-10-01 [^s1] [pinned]
   ```
2. **Commands**:
   - `aft pin <page>:<line_number>`: Appends `[pinned]` to the specified fact line.
   - `aft unpin <page>:<line_number>`: Removes the `[pinned]` marker.
   - `aft pin list`: Lists all pinned facts across the vault.
3. **Injection Guarantee in `src/aft/inject.py`**:
   - In `_block()`, pinned facts are partitioned into a dedicated `# Standing Rules` block placed immediately after the project title.
   - Pinned facts are allocated budget before any other section and are never subjected to `_trim()`.
4. **Immutability from Dreaming**:
   - The dream consolidation step (`src/aft/dream/`) is barred from removing or unpinning a fact. Only explicit human edits or `aft unpin` may alter pinned status.

---

## 2. Terminal UI (`aft tui`) for Headless and SSH Workflows

### Overview
An interactive Terminal User Interface (TUI) built using Python's `textual` or `rich` library, providing a terminal-native equivalent to browsing the Obsidian vault.

### Problem Statement
Afterthought treats Obsidian as its primary UI (`~/afterthought`). However, coding agents often operate inside headless Linux devcontainers, remote SSH servers, or cloud workspaces where running Obsidian Desktop is impossible or inconvenient. Users currently have to resort to raw CLI invocations (`aft notes`, `aft settle`) or plain `cat` and `less`.

### Proposed Design
1. **Command**: `aft tui`
2. **Views**:
   - **Dashboard**: Project selector, latest session summary, in-flight work, and count of open conflicts.
   - **Needs-You Resolver**: Interactive viewer for `reports/needs-you.md`. Shows the conflict note, the supporting verbatim quotes, and provides single-key actions:
     - `k`: Keep standing fact (`aft settle --keep`)
     - `w`: Withdraw fact (`aft settle --withdraw`)
     - `r`: Prompt to enter replacement text (`aft settle --replace "<text>"`)
   - **Vault Browser**: Tree view of `projects/`, `global/`, and `digests/` with markdown syntax highlighting.
   - **Search View**: Interactive fuzzy search backed by SQLite FTS5 (`src/aft/search.py`).
3. **Dependencies**: Evaluate adding `textual` as an optional extra (`uv tool install 'afterthought[tui]'`).

---

## 3. Native Pi Extension Slash Commands

### Overview
Register native slash commands within `integrations/pi/afterthought.ts` via Pi's `ExtensionAPI`, enabling users to inspect, search, and settle memory directly inside the Pi session.

### Problem Statement
Currently, afterthought's Pi adapter (`integrations/pi/afterthought.ts`) is completely headless: it runs on hooks and injects memory at startup. When `notes.count` reports waiting questions (`Needs your decision`), the user must type `"settle"` to tell the agent to run the `aft` skill, or switch to an external shell.

### Proposed Design
Leverage `pi.registerCommand()` in `integrations/pi/afterthought.ts`:
1. **`/aft`**: Displays a status panel in Pi's terminal:
   - Current resolved project and slug.
   - Last session handoff summary.
   - Open conflict count.
2. **`/aft-notes`**: Prints all open questions and conflicts for the current project with their supporting quotes directly to the user's terminal.
3. **`/aft-settle [index] [keep|withdraw]`**: Allows the user to settle a conflict note directly from the Pi command line without consuming agent turns.
4. **`/aft-search <query>`**: Runs `aft search "<query>"` via subprocess and renders the top results in Pi's UI.

---

## 4. Combating Reasoning Entrenchment and Circular Loops

### Overview
Derived from `matthewfl/pi-contemplator`. Prevent agents from falling into repeated debugging loops across turns and sessions.

### Problem Statement
In complex multi-turn sessions, LLM coding agents often formulate flawed hypotheses and repeatedly attempt variations of the same broken approach across 10-20 turns. Naive handoffs record what was tried without explicitly marking what was disproven.

### Proposed Design
1. **Digest Dead-End Extraction**:
   Enhance `src/aft/prompts/digest.md` to extract an explicit list of `disproven_hypotheses` alongside raw failed commands (for example: "Disproven: Issue was NOT caused by database locks; deadlocks were ruled out by transaction logs").
2. **Handoff Negative Guidance**:
   Inject these disproven hypotheses into the next session's `Last session` handoff so a fresh cold session does not repeat the same circular loop.

---

## 5. Context Hygiene and In-Flight Output Pruning

### Overview
Derived from `alpertarhan/pi-smart-compact` (Pi Continuity) and `elpapi42/pi-observational-memory`. Protect live context from oversized tool outputs.

### Problem Statement
Afterthought filters tool outputs post-session during transcript ingestion in `src/aft/filter.py`. However, during a live session in Pi or Claude Code, massive tool outputs (such as 5,000-line test runs or huge diffs) clog the live context window and trigger early, lossy compactions.

### Proposed Design
1. **Pre-Compaction Transcript Trimming**:
   In `integrations/pi/afterthought.ts`, hook into `session_before_compact` to run a local sanitization pass on tool outputs, replacing large blocks with compact summary anchors and file/line references before Pi runs its native compaction pass.
2. **Preserve "The Way Back"**:
   Ensure references to archived evidence remain retrievable via `aft trace <source>` so the agent can inspect full details on demand without keeping raw bytes in working memory.

---

## 6. Memory Store Diagnostics and Health Audits (`aft doctor`)

### Overview
Derived from `Mont3ll/pi-persistent-intelligence`. Implement a comprehensive diagnostic command `aft doctor` (extending `src/aft/lint.py`).

### Problem Statement
As an afterthought vault grows over months across dozens of repositories, silent data drift can accumulate:
- Candidates that never reached the recurrence gate and sat untouched for months.
- Superseded lines that were archived, leaving unresolved footnote citations.
- Code anchors (R8) tied to git branches that were deleted or rebased.
- Fact pages approaching the maximum token budget.

### Proposed Design
1. **Audit Health Metrics**:
   - Count and age distribution of active vs candidate facts.
   - Anchor validity check across default git branches using ripgrep.
   - Footnote citation integrity: assert every `[^id]` in markdown matches a valid entry in the page's YAML `sources` list.
   - Orphaned candidates: report candidate facts older than 60 days that have received zero fresh signals.
2. **Interactive Remediation**:
   - Provide flags such as `aft doctor --prune-candidates` to purge abandoned candidate facts with one clean git commit and report.

---

## 7. Attention Dynamics and Injection Placement

### Overview
Derived from `samfoy/pi-memory`. Optimize token recency and attention for smaller local models.

### Problem Statement
Afterthought injects the entire memory block into Pi's system prompt options. While effective for frontier reasoning models, smaller local models (such as 7B-14B parameters on vLLM or Ollama) suffer from lost-in-the-middle attention degradation when reading long system prompts.

### Proposed Design
1. **Configurable Injection Placement**:
   Add a configuration setting `inject.placement = "system" | "pre-prompt"` in `~/.afterthought/config.toml`.
2. **Behavior**:
   - `"system"` (default): Current behavior, placing memory in the system prompt.
   - `"pre-prompt"`: Attaches the short-term `Last session` handoff and active project state as a user-turn context prefix immediately preceding the user's first prompt, maximizing token recency and model attention on the most urgent working state.

---

## 8. Deferred Review Findings

Open findings from the full code review of commit `36be494`, deferred behind core functionality. Each lists where the problem is, what goes wrong and the intended fix.

### M6. Evidence lives outside the vault and cannot be rebuilt (needs a design decision)
- **Where:** `src/aft/library/propose.py`, `src/aft/claude_memory.py`, `src/aft/dream/apply.py`, `src/aft/dream/rules.py`, `src/aft/evidence.py`, `src/aft/work.py`.
- **Problem:** `~/.afterthought/library/verified_quotes.json` is the only proof for quotes from excluded instruction files and imported Claude memory, and it is outside the vault. The dream re-verifies quotes against the archive, which `aft run` purges after 90 days, so R3 signals older than 90 days count zero and a digest waiting longer than retention loses every fact to R1. A vault cloned to a new machine rejects every waiting library-proposal and Claude-memory digest. DESIGN 8 and 14 promise permanent quotes.
- **Fix:** Commit the verification record in the vault beside the import; record "verified at digest time" on the digest and trust it after evidence expires; never purge evidence a waiting digest needs.

### M13. Temporal parsing misreads common words and filters only evidence
- **Where:** `src/aft/temporal.py`, `src/aft/search.py`.
- **Problem:** Weekday abbreviations match ordinary words ("sun microsystems", "sat solver", "wed to the schema"); "this friday" can resolve to the future; only evidence is time-filtered, not digests; a purely temporal query returns arbitrary rows.
- **Fix:** Require full weekday names or a cue (on, last, since); clamp to the past; filter digests by date; list the period's digests for a purely temporal query.

### M15. `aft forget pattern` does not reach the library's extracted text
- **Where:** `src/aft/forget.py`, `src/aft/index.py`, the cache `~/.afterthought/library/<hash>.json`.
- **Problem:** Redaction rewrites library `*.md` files but the index rows and chunk cache are keyed by the import-time hash, so they are skipped; PDF, txt, html and json files are never redacted; the rewrite breaks recorded hashes so quotes stop verifying. A secret in an imported doc stays searchable after `forget pattern`.
- **Fix:** Redact the cache and rebuild affected library rows; refuse or report edits to library files instead of silently invalidating hashes.

### M16. The digest and current-truth evals score quotes and substrings, not correctness
- **Where:** `scripts/eval_digest.py`.
- **Problem:** Facts match on 60% quote overlap or 40% shared words, so a right quote with a wrong statement scores correct; one run fact can satisfy several key items; `[observed]` facts are never scored; the key is built from the backends under test with no held-out split; `score_block` uses exact substrings; shell backends get a different prompt shape than production.
- **Fix:** Match one-to-one; judge statements for matched pairs; score `[observed]`; freeze a held-out split; paraphrase-tolerant current matching. Do this before the F69 benchmark run.

### M19. Remaining test gaps
- Skip `tests/test_evidence.py` chmod tests when `os.geteuid() == 0`; mark the 150 ms timing test in `tests/test_inject.py`. Confirm the regression tests named for C1, H1-H8 and M12 exist.

### Low findings
- **L1** Recent layer keeps the oldest ten lines (`inject.py`): sort newest first before slicing.
- **L2** Summary schema lacks `goal` and `files`, and `docs/configuration.md` names `base` as default while `config.py` uses `turns`.
- **L3** `explicit_correction` is never set, so R3's correction bypass is unreachable: set it or remove it.
- **L4** `aft settle --replace` writes an ungrounded `[stated]` line dated with the old evidence.
- **L5** The Claude reader ignores `isSidechain`, so subagent prompts read as user turns.
- **L6** Paste detection only counts tagged blocks.
- **L7** `aft inject` passes `never=exclude_paths` while the hook does not, so they can disagree.
- **L8** `vault.commit` commits whatever else is staged: use `git commit --only -- <paths>`.
- **L9** Anchor commits reach `git merge-base` without `--end-of-options`; the `rg -F` call adds nothing over Python `in`.
- **L10** Hot spots: per-lookup digest walk in `dream/apply.py`, full `usage.jsonl` parse per budget check, full newest-session re-read in `reconcile.py`, vault walk per link in `index.py`.
- **L11** Digest metadata keyed by statement collides; duplicated line assignment in `schedule.py`; macOS `ru_maxrss` units in `eval/longmemeval.py`; `json_schema` sent without `strict`.
- **L12** Fenced-code detection exists four times, digest-name parsing four times, and the `[stated]` trust checks twice (`digest.verify`, `apply.settle`); merge the trust checks first.

### Prompt and injection ideas, ranked by expected impact
1. Bind statements to evidence: evidence-first schemas, whole-sentence quotes, exact grounding of names and numbers for every tag; add a statement-correctness check to `eval_digest.py score`.
2. Build a decision eval: fixed (new fact, lines, expected action) cases from real Rejected and Conflicts rows, with a reasoning field first in `decide`.
3. Data framing for small models: wrap untrusted content in tags, state once that it is data, restate the output contract after it; add adversarial synthetic sessions expecting no fact.
4. Stage observed extraction: select spans with a basis enum, then write statements constrained to them; score `[observed]` precision.
5. Few-shot examples in `digest.md` and `turns.md` for question vs decision, paste vs the user's words, and approval with its proposal; A/B test them.
6. Self-consistency for extraction (three samples near T=0.3, keep items in two), with `seed` set whenever T > 0.
7. A/B discipline: held-out split, paired per-session differences with bootstrap intervals, prompt hash, model and temperature in each report, tokens per kept correct fact.
8. Injected block framing: a header saying the lines are recorded notes, not instructions, plus one provenance token and date per line, e.g. `- (you, 2026-10-02) ...` and `- (seen, 2026-09-30) ...`.

---

## 9. Retrieval and Extraction Ideas from MemMachine

Ideas taken from a review of MemMachine (github.com/MemMachine/MemMachine, Apache-2.0). Measure each on the LongMemEval-S harness (`eval/longmemeval.py`, BUILD-PLAN F69, F71, F72) before adopting.

### 9.1 Return neighbouring turns around a hit
- **Source:** `declarative_memory.py` `expand_context`, about a third of the window before the matched turn and two thirds after.
- **Problem:** the matched turn rarely holds the whole answer, which shows in Recall-all@10 (0.846 baseline) and multi-session questions.
- **Design:** `aft search` returns the matched evidence entry with its neighbouring entries from the same session, deterministically and within a character budget.

### 9.2 Index sentences, return the parent turn
- **Source:** MemMachine's sentence-chunk "derivatives".
- **Problem:** a long turn dilutes BM25 for the one sentence that answers, which hurts preference questions (baseline Recall@1 0.47).
- **Design:** index evidence at sentence grain in FTS5 and collapse hits to their parent entry; the index stays a rebuildable cache. Cost: a larger index.

### 9.3 Named profile categories in the extraction prompts
- **Source:** MemMachine's profile tags (`server/prompt/profile_prompt.py`): hobbies and interests, relationship and family context, health and wellness, financial profile, life goals and milestones, media habits, geographic and cultural context, and similar.
- **Problem:** the digest misses personal facts (LongMemEval digest Recall@10 0.80 against 0.98 raw), even after F73 widens the definition of a fact.
- **Design:** list the categories in `turns.md` and `digest.md` as what a fact about the user covers, so the model scans for each. Facts still need a verified quote. Use neutral examples, never ones from an evaluation set.
