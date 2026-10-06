# Commands

Every command is a subcommand of `aft`. `aft <command> --help` lists its options, and
`aft --version` prints the version. A command that fails prints the reason and exits 1.

## Setup

| Command | What it does |
|---|---|
| `aft init` | Writes `~/.afterthought/config.toml` with the defaults if there is none, and creates the vault as a git repository. Safe to run again. |
| `aft install claude [--dry-run] [--uninstall]` | Adds afterthought's `SessionStart`, `PreCompact` and `SessionEnd` hooks to `~/.claude/settings.json`, and installs the `aft` skill, which tells an agent how to use afterthought's commands. A copy of the skill under an earlier name (`library-import`, `aft-library-import`) is removed when it is one afterthought shipped. Leaves every other hook alone. `--dry-run` shows the change; `--uninstall` removes only afterthought's entries. |
| `aft install pi [--dry-run] [--uninstall]` | Copies afterthought's extension into pi's extensions directory and installs the `aft` skill in pi's skills directory. |
| `aft install timer [--dry-run] [--uninstall]` | Writes the systemd user units that run `aft run` every `dream.interval_minutes`, then enables and starts the timer. `--uninstall` stops and removes them. |

## Projects

| Command | What it does |
|---|---|
| `aft project add [path] [--slug S] [--to SLUG] [--title T]` | Registers the directory at `path` (default: the current one) as a project, git repository or not. A repository's `origin` remote is recorded too, so a re-clone finds the same memory. With `--to SLUG` the directory joins the registered project SLUG instead of making a new one, and is refused when another project already holds it. A path under the system temporary directory is refused unless `--allow-temp` is given. |
| `aft project list [--json]` | Lists registered projects, their ids, paths and remotes. |
| `aft project rename OLD NEW` | Changes a project's slug. Its id, and every page that names it, stay the same. |
| `aft project merge FROM INTO` | Merges the project FROM into INTO: INTO takes FROM's paths and remotes, its digests, its archive, and its pages, with a page both hold combined line by line under its matching sections, and FROM leaves the registry. |
| `aft project forget SLUG` | Removes a project from the registry. Its pages stay in `projects/<slug>/`, and the command says so; delete them yourself if you want them gone. |
| `aft project archive SLUG` | Retires a project from everyday memory with the user's say-so: its block is not injected, and its pages and digests answer only `aft search --archive`. Sessions still capture and dream, and the daily report names projects idle for 90 days. Nothing is deleted; `aft project unarchive` brings it back. |
| `aft project unarchive SLUG` | Ends a project's archive: its block is injected again and its pages and digests answer ordinary searches, from the date the archive began. |
| `aft key [path] [--json]` | Shows which project a directory belongs to and why; exits 1 when it belongs to none. |

## Memory

| Command | What it does |
|---|---|
| `aft inject [path]` | Prints the memory block a session in `path` starts with. Reads files only and calls no model. |
| `aft search "WORDS" [--project P] [--archive] [--reports] [--limit N] [--json]` | Searches every project that is not archived, with BM25 ranking inside a fixed order: the current directory's project first, the global pages next, the other projects and the unfiled text last; each hit names its project. `--project P` narrows to P and the global pages; `--all` is accepted and changes nothing; `--archive` adds every project's superseded and stale lines and resolved notes; `--reports` adds dream reports. |
| `aft read PATH [--level outline\|abstract\|full]` | Prints a page at the depth a task needs. |
| `aft trace PAGE SOURCE` | Prints a fact's quote and the session entries around it. With an evidence hit's `aft://session/<agent>/<id>#<entry>` path, it opens that entry. |
| `aft notes [path] [--all] [--json]` | Lists the notes waiting on your decision for `path`'s project and the global and reference pages: each line, its note, the quotes behind both with their dates, and the `aft settle` command for it. `--all` lists every project's. Reads files only. |
| `aft settle WHERE --keep\|--withdraw\|--replace "STATEMENT"` | Settles the note under the line at `WHERE`, the `<page>:<line>` that `aft notes` prints. `--keep` leaves the line standing, `--withdraw` strikes it through, and `--replace` supersedes it with `STATEMENT`, cited to the note's own quotes (tagged `[stated]` when one is your words, `[observed]` otherwise; refused when none still verifies, or when only a worker session's do). The note moves word for word to the page's archive, `reports/needs-you.md` is regenerated, and the change is one vault commit. |
| `aft review <project\|global> [--withdraw N,N,...\|all]` | Asks the model which of a scope's active memory lines its keep-test would not keep: every active line of the scope's fact pages (`recent.md` included), about twenty lines per call, and saves the numbered lines it would withdraw, each with a one-line reason, to `reports/review-<scope>.md`. Nothing changes until you order it: `--withdraw` strikes the chosen numbers the way `aft settle --withdraw` does, skips and names a line that changed since the list was saved or has an open note, records what became of each line in the report, and is one vault commit. A number the list does not carry refuses the whole withdrawal. The calls count against the daily token budget. |
| `aft lint` | Checks every page against the page format: OKF fields, the fact-line grammar, citations that resolve. |
| `aft reindex` | Rebuilds the search index from the vault's files and the evidence archive. |
| `aft eval [GOLDEN] [--limit N]` | Checks memory against a golden set of known answers you write, by default `<vault>/golden.yaml`; a line passes when it is found in the top `N` search results (default 8). |

## Background work

The timer runs `aft run`, which ends with a dream. Both can be run by hand.

| Command | What it does |
|---|---|
| `aft run [--force]` | Sweeps for sessions no hook reported, then digests the queue until the day's token budget is spent, then dreams. Exits 1 when a job could not be digested, and 0 when the budget simply ran out. While paused it says so and does nothing, unless `--force` is given. |
| `aft dream [--force]` | Turns waiting digests into memory, one quiet project at a time, with one commit and one report section per project. Exits at once when no digest is waiting, or while paused unless `--force` is given. |
| `aft budget` | Shows today's token use against `work.daily_token_budget`, what is left, and when the count starts again. |
| `aft budget reset` | Starts today's count again from now, for when something spent the budget and has been fixed. The usage log is never changed; midnight UTC still starts the next day. |
| `aft model cooldown [--clear]` | Shows the model endpoint's cooldown, how long is left and why it was set: a server error, a rate limit, or an endpoint that could not be reached. While it stands, model calls fail without touching the network and `aft run` stops before the queue. `--clear` ends it, as does `aft budget reset`. |
| `aft pause [REASON]` | Pauses background work: `aft run` and `aft dream` do nothing, whatever starts them, and a session's end no longer starts a run. Sessions are still captured and queued, and the memory block is still shown, so nothing is lost. Use it while the model server is down, while something in the pipeline looks wrong, or during a large edit of the vault. |
| `aft resume` | Ends the pause and starts a run for the backlog. |
| `aft redigest SESSION` | Rebuilds a session's digest from the evidence archive, with the current prompt, checks and redaction patterns. The rebuilt digest is dreamed again. Counts against the daily budget. `aft redigest --unfiled [--dry-run]` is the backfill: every still-archived session whose digests sit under `_unfiled` is rebuilt to file under the project its recorded directory now resolves to; `--dry-run` lists the sessions and the tokens it would charge and writes nothing. |

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
