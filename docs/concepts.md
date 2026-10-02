# Concepts

This page explains what afterthought keeps, how something becomes memory, and what you
will see from day to day. The reasons behind each choice are in
[DESIGN.md](../planning/DESIGN.md).

## From conversation to memory

Material moves through four stages. Each step up has a check that a script, not a model,
performs.

1. **Evidence.** When a session ends (or compacts), its transcript is read, filtered and
   stored in the evidence archive, `~/.afterthought/evidence.sqlite`. The filter drops
   routine tool output, keeps errors, and redacts secrets: API keys, tokens, passwords, and
   anything matching your own `capture.redact_patterns`. Evidence is what happened. It is
   kept so claims can be checked, and it is searchable, but it is not memory.
2. **Digest.** A model reads the session in chunks and writes a digest page: a short
   summary, a handoff (what was in flight, what failed, what is still open), and proposed
   facts. Each fact must quote the session word for word. The script looks for the quote
   in the archived entry it cites and drops the fact when it is not there. An answer that
   cites entries the chunk does not hold, fills fields with placeholders, or has two or
   more facts whose quotes the session never said, is asked for again and then refused
   whole: that part of the session adds nothing, and the page records it as refused,
   while the rest of the session is kept. Digests live under `digests/` in the vault and
   are searchable, but nothing in a digest is trusted memory yet.
3. **Dream.** A background run takes each verified fact, finds the related lines already
   in memory, and asks the model for one decision: add it, confirm an existing line,
   supersede one, mark a conflict, or skip it. Fixed rules then decide whether that
   decision may stand. The script, never the model, edits the pages, and each run is one
   git commit with a report saying what changed and why.
4. **Memory.** The lines on the project's pages and your global pages. They are what an
   agent is shown at session start and what `aft search` finds first.

## Source tags

Every line of memory carries one tag saying where it came from:

| Tag | Means |
|---|---|
| `[stated]` | You asserted it in your own words: a fact, a choice, a preference, or a standing rule such as "never push to main". |
| `[observed]` | Seen in a file, a command's output, a log or an error. |
| `[inferred]` | The model's own conclusion from a session. |
| `[suggested]` | An idea or a tentative remark nobody committed to. |

A request for the task at hand ("run the tests") is not a fact at all, and a question is
not `[stated]`. Text you paste is not your own words either: a pasted log or error is
`[observed]`, and any other pasted claim, such as a prompt or another agent's message, is
`[inferred]`.

`[inferred]` and `[suggested]` facts wait in a page's Candidates section until they recur:
three signals across two separate sessions. They never replace a line on their own.

## Long-term and short-term memory

- **Long-term memory** is what will still be true and useful the next time someone opens
  the project, even a year from now: decisions and their reasons, preferences, how things
  are set up, lessons, procedures. It lives on `projects/<slug>/overview.md` and the
  global pages. A long-term line never ages out. It leaves only when newer evidence
  supersedes it, its anchor in the code stops matching, or you remove it with `aft forget`.
- **Short-term memory** is what the project's last few sessions were working with: who is
  doing what, what is paused and why, a setting changed for a test. It lives on
  `projects/<slug>/recent.md` and is shown in its own "Recent" section of the memory block.
  It is counted in sessions, never in days: a line rolls off once it has not come up in
  the project's last `dream.short_term_sessions` sessions (5 by default). A project you
  leave alone for a year comes back with its short-term memory as it was, dated so the
  agent can judge it.
- **Neither** is what a session did and where it stopped. That goes in the digest's
  handoff, and the newest handoff is shown as "Last session".

A short-term line becomes long-term when it outlasts the window (its sources are at least
`short_term_sessions` sessions apart) or when a later session states the same thing as a
long-term fact.

## Who may change what

Newer evidence replaces older memory only with enough authority:

- A preference or decision changes only when you state something newer.
- How things are set up (state, facts, lessons, procedures, references) also changes when
  a later session observes something newer.
- A model's inference never replaces anything.

Evidence without that authority does not overwrite: the line stays, a `[!conflict]` note
is added under it, and the disagreement shows in `reports/needs-you.md`. When something
you observed contradicts one of your own decisions, the line gets a `[!question]` for you
to answer.

## Projects

A session belongs to the project of the directory it started in. A registered path wins,
then a git repository's `origin` remote (so a re-clone keeps its memory), then an
unregistered repository named after its directory. A session in a directory that belongs
to no project is still captured: it is filed as "unfiled", and only its global facts (what
you say about yourself and your machines) can become memory. Register a directory with
`aft project add` to give it memory of its own.

## Worker sessions and exclusions

Sessions under `capture.worker_paths`, such as orchestrator worktrees, are evidence of a
lower rank: archived and searchable, digested only with spare budget, and never promoted
to memory without a primary session's support. Sessions under `capture.exclude_paths`, or
of a project in `capture.exclude_projects`, are never captured at all.

## Off the record

Start a prompt with `aft off` (or "off the record") and nothing from that point is
captured, until a prompt starts with `aft on` (or "on the record"). The markers only count
at the very start of a prompt, so talking about them triggers nothing.

## Day to day

- Each session starts with the memory block: the project's name, the last session's
  handoff, its long-term facts, your global preferences, its recent short-term facts, and
  pointers to its other pages. The block stays within `inject.max_chars`.
- Agents can run `aft search` and `aft read` for anything the block does not hold.
- The timer digests finished sessions and dreams once a project has been quiet for
  `dream.quiet_minutes`. Work stops for the day when `work.daily_token_budget` is spent
  and resumes after midnight UTC.
- Read the day's dream report in `reports/` to see what changed, and `reports/needs-you.md`
  for anything waiting on you.
- `aft pause` stops background work without losing anything: sessions keep being queued,
  and `aft resume` works through them.
