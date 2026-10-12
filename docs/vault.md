# The vault

The vault is where memory lives: plain markdown in its own git repository, which is also
an Obsidian vault. It is `~/afterthought` by default (`vault` in
[configuration](configuration.md)). The machinery, meaning the config file, the evidence
archive, the search index, the capture queue, locks and logs, lives separately in
`~/.afterthought`, so backing up the vault never includes runtime state and updating the
code never touches memory.

## Layout

```text
~/afterthought/
  index.md                 generated root of the vault
  log.md                   a line per dream run
  registry.yaml            projects, their ids, paths and remotes
  global/
    preferences.md         your preferences that apply everywhere
    environment.md         your hosts, tools and services
  projects/<slug>/
    overview.md            the project's long-term memory
    recent.md              its short-term memory
    index.md               generated list of its pages
  digests/<slug>/<yyyy>/<mm>/<date>-<agent>-<session>.md
                           one page per digested part of a session
  digests/_unfiled/...     digests of sessions that belong to no project
  phrases/<page or digest path>.json
                           the search phrases the model wrote for that page's lines
                           and that digest's sections; generated, never edited by hand
  archive/<page path>      resolved notes, and superseded lines older than 90 days
  references/<name>/       reference pages for an imported library collection
  library/<name>/          the collection's files
  reports/
    needs-you.md           every open conflict and question, across projects
    <yyyy>/<mm>/<date>.md  the day's dream report, one section per run
```

Open `~/afterthought` as a vault in Obsidian to browse it. Reports are listed under
Obsidian's Excluded files, so they stay out of its graph and search while you can still
open them.

## Reading a page

A memory page is a set of sections named for the kinds of fact they hold: Preferences,
Decisions, State, Facts, Lessons, Procedures, References, and Candidates for facts still
waiting to recur. Every line follows one grammar:

```markdown
- The capture hook enqueues work and returns. [observed] 2026-09-30 [^s31]
```

The statement, its source tag, the date it was last evidenced, and footnotes naming its
sources. Each source in the page's frontmatter carries the verbatim quote it rests on and
the session entry it came from; `aft trace <page> <source>` shows the conversation around
it.

Lines carry their state in the text:

| Looks like | Means |
|---|---|
| A plain line | Active: shown to agents. |
| `~~struck~~ ... superseded <date> by [^id]` | Replaced by newer evidence. Kept, not shown. |
| `~~struck~~ ... withdrawn <date>` | Removed with `aft forget`. |
| `... (stale <date>: <reason>)` | Its anchor in the code no longer matches. Not shown. |
| A line followed by `> [!conflict]` | Disputed: newer evidence disagrees without the authority to replace it. Shown, marked as disputed. |
| A line followed by `> [!question]` | Waiting on you. Shown, marked as waiting. |

## Editing by hand

You can edit any page. The dream waits until the vault has had no uncommitted change for
`dream.quiet_minutes`, then commits your edits on their own as `checkpoint: human edits`
before it does anything else.

- **Add a fact** by typing a bare bullet, `- No RGB lighting.`, under the right section.
  The next run turns it into a full line tagged `[stated]`, sourced to your edit.
- **Answer a question or settle a conflict** by typing under the note. Notes are never
  deleted: a resolved note moves, as written, to the page's archive.
- **Promote short-term memory** by moving a line from `recent.md` to `overview.md`, or drop
  one by deleting it.
- Text you type that the tool cannot parse is never rewritten or lost: an edit the tool
  would have to make through it is refused and reported instead.

Run `aft lint` after a large edit to check the pages still parse.

## Reports and needs-you

Each dream run that changes anything appends a section to the day's report in
`reports/<yyyy>/<mm>/<date>.md`, always in the same order: a summary, what needs you, what
was superseded, added and confirmed, conflicts, what the rules rejected and why, stale
lines, candidate progress, short-term changes, and the command to revert the run.

`reports/needs-you.md` is regenerated on every run and every `aft settle`: one list of every
open conflict and question across all projects. Sessions raise the same notes for their
project and the global and reference pages, and `aft settle` closes them: the note moves word for word to
`archive/<page path>`, and the line is kept, withdrawn, or superseded by a statement that
rests on the note's own quotes.

Reports describe memory; they are never memory. Agents are not shown them, default search
skips them, and the dream never reads them.

## Digests

A digest page records one session's part as the model read it: its `description` is the
summary, `aft.handoff` is what the next session is told was in flight, failed or open,
and its lines are the facts that passed quote verification. Two records explain what it
left out:

- `aft.dropped`: each proposed fact the script refused, with the reason (a quote not in its
  entry, `[stated]` on the agent's own turn, and so on).
- `aft.refused_parts`: each part of the session whose answer was refused twice because it
  did not match the text it was given, with that part's first and last entry. The rest of
  the session is kept; `aft redigest` reads a refused part again.

A digest is evidence of what a session said, not memory: the dream decides what becomes
memory, and a digest's content changes only when you run `aft redigest` or `aft forget`.

## Reverting

Every dream run is one git commit in the vault, and its report section names it. To undo a
run:

```bash
git -C ~/afterthought revert <commit>
```

Reverting a run also clears the consumed mark on the digests it took up, so the next run
dreams them again. To stop that, run `aft pause` first, decide what should happen to those
digests (`aft redigest` rebuilds one from its evidence), then `aft resume`.

## Forgetting

- `aft forget session <id>` removes one session from everything Afterthought controls and
  withdraws the memory lines that rested only on it.
- `aft forget pattern <regex>` redacts text everywhere Afterthought copied it.
- `aft project forget <slug>` removes a project from the registry and leaves its pages for
  you to delete.

None of these rewrite the vault's git history. Older commits still hold what was removed;
`aft forget` explains how to rewrite history if you need it gone from there too.
