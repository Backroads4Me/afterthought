---
name: aft
description: Afterthought, the user's shared memory for coding agents, run through the aft command. Use when the user asks what memory holds or what agents remember, to search or read memory, to settle the notes memory has waiting on the user's decision, to review what memory keeps, to forget a session or redact text, to rebuild a digest, to pause or resume background work, to check or reset the token budget, to register, rename or remove a project, or to import reference documentation or a specification collection into the afterthought library.
---

# aft

Afterthought keeps the user's memory: curated facts in a vault of markdown pages, built in
the background from past sessions. You reach it only through the `aft` command. Never
edit vault pages yourself; when the user wants to change memory by hand, tell them how
(see "Changing memory by hand" below).

Add --help to any aft command to see its options.

## Commands you may run without asking

These read and change nothing:

| The user wants | Run |
|---|---|
| To find something in memory | `aft search <words>`; add `--all` for every project, `--archive` for superseded lines, `--reports` for dream reports |
| To read a page | `aft read <path>`, with `--level outline` or `--level abstract` for less |
| Where a fact came from | `aft trace <page> <source>`: the page's path and the footnote id the fact cites, such as s3; or a search hit's evidence path on its own |
| What a session starts with | `aft inject <path>` |
| Which project a directory is | `aft key <path>` |
| The notes waiting on their decision | `aft notes`; add `--all` for every project, `--json` for the fields |
| The day's token use | `aft budget` |
| The registered projects | `aft project list` |
| A check of the vault | `aft lint` |
| A check against known answers | `aft eval` |

## Changes you can undo: ask first

These change memory, configuration or background work, and each can be undone. Say exactly
what you will run and what it will do, and run it only once the user agrees. Where a
command has `--dry-run`, show its output first.

| The user wants | Run |
|---|---|
| To stop background work for a while | `aft pause <reason>`; sessions are still queued |
| To start it again | `aft resume` |
| To run background work now | `aft work`, or `aft dream` for the dream alone |
| To start the day's token count again | `aft budget reset` |
| To rebuild a session's digest | `aft redigest <session>`; it counts against the budget, and the vault's git history keeps the old digest |
| To backfill the sessions of no project | `aft redigest --unfiled`, with `--dry-run` first; it rebuilds every still-archived unfiled session to file under the project its recorded directory now resolves to, and counts against the budget |
| To register a directory | `aft project add <path>`; any directory is a project now, git or not |
| To rename a project | `aft project rename <old> <new>` |
| To archive a project | `aft project archive <slug>`; its memory block is not injected and its pages and digests answer only `aft search <words> --archive`, until `aft project unarchive <slug>` |
| To bring an archived project back | `aft project unarchive <slug>`; its pages and digests answer ordinary searches again, from the date the archive began |
| To settle a note waiting on them | `aft settle <where> --keep`, `aft settle <where> --withdraw`, or `aft settle <where> --replace <statement>`, where is the note's page and line as `aft notes` prints them; follow "Notes waiting on the user" below |
| To ask what memory should not keep | `aft review <project\|global>`; it calls the model and counts against the day's token budget. Striking the lines it names, `aft review <project\|global> --withdraw <n,n,...\|all>`, follows "Reviewing what memory keeps" below |
| To rebuild the search index | `aft reindex` |
| To create the config file and the vault | `aft init`; safe to run again |
| To install or remove afterthought's hooks, extension, skill or timer | `aft install claude`, `aft install pi`, `aft install timer`; each takes `--dry-run` to preview and `--uninstall` to remove |
| To import Claude Code's own memory files | `aft import claude-memory`, with `--dry-run` first |
| To import documentation into the library | read `library-import.md` beside this file and follow it step by step |

## Changes you cannot undo: name exactly what goes

Before any of these, tell the user exactly what will be removed (the session id, the
pattern, or the project) and what they lose, and run the command only after an explicit
yes to that specific command. An earlier general go-ahead does not count.

| The user wants | Run | What is lost |
|---|---|---|
| To remove a session from memory | `aft forget session <session>` | its evidence, digests, and the memory lines resting only on it |
| To redact text everywhere | `aft forget pattern <regex>` | every match, in the archive, digests, pages and reports |
| To remove a project | `aft project forget <slug>` | its registration; re-added, it gets a new id, so its old pages no longer attach to it, and they stay until deleted by hand |

Never run `aft hook`: the agents' own hooks call it.

A prompt starting with "aft off", "aft on", "off the record" or "on the record" sets what
afterthought keeps; it is not a request for an aft command. Acknowledge it briefly and
continue with what the rest of the prompt asks.

## Notes waiting on the user

When newer evidence disagrees with a line in memory and has no authority to replace it,
memory keeps the line and writes a note under it: a conflict, or a question. The line then
waits on the user's decision. When notes wait, the memory block a session starts with says
how many under "Needs your decision". Before anything else in that session, tell the user
how many notes wait, and that replying "settle" goes through them now and any other reply
skips them. "settle" is the whole answer: start step 1. Skipping changes nothing; the next
session asks again.

To settle them:

1. Run `aft notes --json`. Each note gives the line memory holds now, the note's own text,
   and the quotes behind both, with their dates and whether each is the user's own words.
2. Take the notes one at a time. In plain words, say what memory holds, what the newer
   evidence says, and when each was said. Recommend one of the three choices, with your
   reason, and ask the user to choose:
   - Keep: the line stands as it is.
   - Replace: a new statement takes the line's place, resting on the note's quotes. Draft
     the statement from those quotes and let the user change it. Replace is refused when
     none of the note's quotes still verifies, or only a worker session's do; offer keep
     or withdraw then.
   - Withdraw: the line is struck through, and nothing replaces it.
3. Run `aft settle <where> --keep`, `aft settle <where> --withdraw`, or
   `aft settle <where> --replace <statement>`, using the `where` field of the note the user
   decided. Each settle is one commit in the
   vault, so `git revert` undoes it, and the note itself moves word for word to the page's
   archive.
4. When the user wants to stop partway, stop. The notes left wait for the next session.

## Reviewing what memory keeps

Memory written before the keep-test existed holds lines it would not keep: progress on
the work, the status of a pull request, what a change did to the code. `aft review
<project|global>` puts the scope's active memory lines to the model, about twenty lines
per call, and saves the numbered lines it would withdraw, each with a one-line reason,
to `reports/review-<scope>.md`. No line changes.

1. Run `aft review <project|global>`, where the placeholder is a registered project's
   slug, or `global` for the global pages. It calls the model and counts against the
   day's token budget.
2. Read the report to the user: each numbered line with its reason. The lines the model
   did not name stay as they are.
3. If the user chooses lines to strike, run `aft review <project|global> --withdraw
   <n,n,...|all>`, with the numbers from the report. A line that changed since the list
   was saved, or one with an open note, is skipped and named; `all` means every line the
   list carries that nothing was yet recorded for. The withdrawal is one commit in the
   vault, so `git revert` undoes it.
4. When the user wants no withdrawal, stop. The list stands in the report for a later
   decision; a later `aft review` saves a new list.

## Changing memory by hand

Memory pages live in the vault, `~/afterthought` by default, and the user may edit them in
any editor or in Obsidian:

- A fact: a bare bullet such as `- No RGB lighting.` under the right section. The next
  dream run turns it into a full line tagged `[stated]`.
- An answer to a question or a conflict: typed under the note, for the next dream run to
  read. Settling it with `aft settle` takes effect at once.
- Short-term memory: a line moved from `projects/<slug>/recent.md` to `overview.md` becomes
  long-term; a line deleted is dropped.

`reports/needs-you.md` lists everything memory is waiting on the user for.
