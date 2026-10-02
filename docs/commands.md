# Commands

Every command is a subcommand of `aft`. `aft <command> --help` lists its options, and
`aft --version` prints the version. A command that fails prints the reason and exits 1.

## Setup

| Command | What it does |
|---|---|
| `aft init` | Writes `~/.afterthought/config.toml` with the defaults if there is none, and creates the vault as a git repository. Safe to run again. |
| `aft install claude [--dry-run] [--uninstall]` | Adds afterthought's `SessionStart`, `PreCompact` and `SessionEnd` hooks to `~/.claude/settings.json`, and installs the `aft` skill, which tells an agent how to use afterthought's commands. A copy of the skill under an earlier name (`library-import`, `aft-library-import`) is removed when it is one afterthought shipped. Leaves every other hook alone. `--dry-run` shows the change; `--uninstall` removes only afterthought's entries. |
| `aft install pi [--dry-run] [--uninstall]` | Copies afterthought's extension into pi's extensions directory and installs the `aft` skill in pi's skills directory. |
| `aft install timer [--dry-run] [--uninstall]` | Writes the systemd user units that run `aft work` every `dream.interval_minutes`, then enables and starts the timer. `--uninstall` stops and removes them. |

## Projects

| Command | What it does |
|---|---|
| `aft project add [path] [--slug S] [--title T]` | Registers the directory at `path` (default: the current one) as a project, git repository or not. A repository's `origin` remote is recorded too, so a re-clone finds the same memory. A path under the system temporary directory is refused unless `--allow-temp` is given. |
| `aft project list [--json]` | Lists registered projects, their ids, paths and remotes. |
| `aft project rename OLD NEW` | Changes a project's slug. Its id, and every page that names it, stay the same. |
| `aft project forget SLUG` | Removes a project from the registry. Its pages stay in `projects/<slug>/`, and the command says so; delete them yourself if you want them gone. |
| `aft key [path] [--json]` | Shows which project a directory belongs to and why; exits 1 when it belongs to none. |

## Memory

| Command | What it does |
|---|---|
| `aft inject [path]` | Prints the memory block a session in `path` starts with. Reads files only and calls no model. |
| `aft search "WORDS" [--project P] [--all] [--archive] [--reports] [--limit N] [--json]` | Searches memory, digests, evidence and library text with BM25 ranking, scoped to the current directory's project and the global pages. `--all` searches every project; `--archive` adds superseded and stale lines and resolved notes; `--reports` adds dream reports. |
| `aft read PATH [--level outline\|abstract\|full]` | Prints a page at the depth a task needs. |
| `aft trace PAGE SOURCE` | Prints a fact's quote and the session entries around it. With an evidence hit's `aft://session/<agent>/<id>#<entry>` path, it opens that entry. |
| `aft notes [path] [--all] [--json]` | Lists the notes waiting on your decision for `path`'s project and the global pages: each line, its note, the quotes behind both with their dates, and the `aft settle` command for it. `--all` lists every project's. Reads files only. |
| `aft settle WHERE --keep\|--withdraw\|--replace "STATEMENT"` | Settles the note under the line at `WHERE`, the `<page>:<line>` that `aft notes` prints. `--keep` leaves the line standing, `--withdraw` strikes it through, and `--replace` supersedes it with `STATEMENT`, cited to the note's own quotes (tagged `[stated]` when one is your words, `[observed]` otherwise; refused when none still verifies). The note moves word for word to the page's archive, `reports/needs-you.md` is regenerated, and the change is one vault commit. |
| `aft lint` | Checks every page against the page format: OKF fields, the fact-line grammar, citations that resolve. |
| `aft reindex` | Rebuilds the search index from the vault's files and the evidence archive. |
| `aft eval [GOLDEN] [--limit N]` | Checks memory against a golden set of known answers you write, by default `<vault>/golden.yaml`; a line passes when it is found in the top `N` search results (default 8). |

## Background work

The timer runs `aft work`, which ends with a dream. Both can be run by hand.

| Command | What it does |
|---|---|
| `aft work [--force]` | Sweeps for sessions no hook reported, then digests the queue until the day's token budget is spent, then dreams. Exits 1 when a job could not be digested, and 0 when the budget simply ran out. While paused it says so and does nothing, unless `--force` is given. |
| `aft dream [--force]` | Turns waiting digests into memory, one quiet project at a time, with one commit and one report section per project. Exits at once when no digest is waiting, or while paused unless `--force` is given. |
| `aft budget` | Shows today's token use against `work.daily_token_budget`, what is left, and when the count starts again. |
| `aft budget reset` | Starts today's count again from now, for when something spent the budget and has been fixed. The usage log is never changed; midnight UTC still starts the next day. |
| `aft pause [REASON]` | Pauses background work: `aft work` and `aft dream` do nothing, whatever starts them, and a session's end no longer starts a run. Sessions are still captured and queued, and the memory block is still shown, so nothing is lost. Use it while the model server is down, while something in the pipeline looks wrong, or during a large edit of the vault. |
| `aft resume` | Ends the pause and starts a run for the backlog. |
| `aft redigest SESSION` | Rebuilds a session's digest from the evidence archive, with the current prompt, checks and redaction patterns. The rebuilt digest is dreamed again. Counts against the daily budget. |

## Forgetting

| Command | What it does |
|---|---|
| `aft forget session SESSION` | Removes one session from the evidence archive, the capture queue, the index and the digests, withdraws memory lines that rested only on it, and scrubs its quotes from page sources, reports and `needs-you.md`. The vault's git history still holds older commits; the command explains how to rewrite it. |
| `aft forget pattern REGEX` | Replaces every match with `[redacted]` in the archive, digests, pages and reports, and prints the line that adds the pattern to `capture.redact_patterns`. |

## Library

Reference documentation imported as a searchable, cited library. The `aft` skill that
`aft install claude` adds drives these for you.

| Command | What it does |
|---|---|
| `aft library survey PATH [--json]` | Surveys a collection directory before import. |
| `aft library validate MANIFEST [--path P]` | Checks a collection manifest. |
| `aft library import MANIFEST [--path P]` | Imports a collection into the vault. |
| `aft library check-map MANIFEST [MAP ...] [--path P]` | Verifies the claims in an imported index or map file. |
| `aft library describe MANIFEST FILE [--path P] [--dry-run]` | Verifies document descriptions and adds them to reference pages. |
| `aft library propose MANIFEST FILE [--path P] [--dry-run]` | Proposes candidate facts from a collection's notes and instruction files, each with a verified quote. Also spelled `propose-facts`. |

## Import

| Command | What it does |
|---|---|
| `aft import claude-memory [--from DIR] [--project SLUG] [--dry-run]` | Imports Claude Code's own memory files as candidate digests, so the dream can judge them like any other evidence. |

## Agents

| Command | What it does |
|---|---|
| `aft hook AGENT EVENT` | Handles one agent lifecycle event read from stdin. The installed hooks and the pi extension call it; you do not run it yourself. |
