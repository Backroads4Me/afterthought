---
name: aft
description: Afterthought, the user's shared memory for coding agents, run through the aft command. Use when the user asks what memory holds or what agents remember, to search or read memory, to forget a session or redact text, to rebuild a digest, to pause or resume background work, to check or reset the token budget, to register, rename or remove a project, or to import reference documentation or a specification collection into the afterthought library.
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
| To register a directory | `aft project add <path>` |
| To rename a project | `aft project rename <old> <new>` |
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

## Changing memory by hand

Memory pages live in the vault, `~/afterthought` by default, and the user may edit them in
any editor or in Obsidian:

- A fact: a bare bullet such as `- No RGB lighting.` under the right section. The next
  dream run turns it into a full line tagged `[stated]`.
- An answer to a question or a conflict: typed under the note.
- Short-term memory: a line moved from `projects/<slug>/recent.md` to `overview.md` becomes
  long-term; a line deleted is dropped.

`reports/needs-you.md` lists everything memory is waiting on the user for.
