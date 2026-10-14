# Afterthought Roadmap Proposals

This document tracks evaluated design proposals and feature candidates for the afterthought roadmap beyond the initial v1 build.

---

## Priority: Detailed Personal Memory Toggle (`memory.personal`)

**On hold** (Ted, 2026-10-13). It waits on M16, whose scorer and held-out split its per-level evals need. When it resumes it ranks ahead of sections 1-7, the deferred Low findings and the prompt ideas in section 8.

### Overview
A setting for users whose use is not mainly coding: `memory.personal = "basic"` (default) keeps the short user profile of BUILD-PLAN F76, and `"detailed"` keeps everything about the user's life (activities, purchases, dated events and quantities, people, places, tastes) and gives non-project memory more weight.

### Proposed Design
- **One prompt per stage**, with the single "what a fact about the user covers" paragraph chosen by the setting; never a forked prompt set, which would double tuning and evaluation and drift.
- **`detailed` weighting in code:** personal pages are injected before project sections with a larger share of `inject.max_chars`, and rank with project memory in search rather than in the global tier.
- **Evals per level:** `basic` holds coding precision and raises profile recall on the synthetic set; `detailed` targets personal recall on the synthetic set, confirmed on LongMemEval-S.
- **Switching** applies to new sessions; earlier sessions are re-extracted only while their evidence is in the archive.

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

## 8. Review Findings

Findings from the full code review of commit `36be494`. All are resolved; each line names the task that did it. The v1 review of 2026-10-14 (`.board/v1-review.md`) found nothing blocking.

- **M6** Evidence outside the vault: F75 (a digest's quote check at digest time is trusted after evidence expires).
- **M13** Temporal parsing: M13 (#174).
- **M15** `aft forget pattern` and the library's extracted text: M15 (#173).
- **M16** Evals scored overlap, not correctness: M16 (#183).
- **M19** Test gaps: M19 (#184).
- **L1, L4** T41. **L5** T40. **L8** T44. **L2, L7, L9, L11** T48 (#188); `json_schema` stays without `strict`, since six response schemas do not meet strict mode's rules. **L3, L12** T49 (#187). **L10** T51 (#189).
- **L6** Paste detection counts only tagged blocks, and stays so: a paste the client does not tag has no reliable mark, and a heuristic that guesses one would refuse the user's own words; the prompts say pasted material is never the user's, and R2 grounds every `[stated]` quote.

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
- **Status (2026-10-09): tried in F76 and not adopted.** Listing the categories in `turns.md` and `digest.md` lowered coding recall on the `eval_digest.py` key (30 and 35 of 76 found against 37-42) and added no profile recall over F73's wording, so the prompts keep F73's text.
- **Source:** MemMachine's profile tags (`server/prompt/profile_prompt.py`): hobbies and interests, relationship and family context, health and wellness, financial profile, life goals and milestones, media habits, geographic and cultural context, and similar.
- **Problem:** the digest misses personal facts (LongMemEval digest Recall@10 0.80 against 0.98 raw), even after F73 widens the definition of a fact.
- **Design:** list the categories in `turns.md` and `digest.md` as what a fact about the user covers, so the model scans for each. Facts still need a verified quote. Use neutral examples, never ones from an evaluation set.
