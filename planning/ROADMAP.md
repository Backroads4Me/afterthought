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
