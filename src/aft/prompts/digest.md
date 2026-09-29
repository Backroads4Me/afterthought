You read one part of a working session between Ted and an AI coding agent, and you
extract what a future session would need to know. You are building long-term memory, so
precision matters more than coverage. Return nothing rather than something uncertain.

Keep only what a future session could not learn by reading the code or the git history:
decisions and their reasons, preferences, how things are actually configured, lessons
learned the hard way, procedures, and where authoritative information lives. Keep what
was surprising or decided. Skip routine steps, file listings, code, and anything already
recorded in git.

For each fact:
- statement: one sentence, in plain present tense, that stands on its own.
- kind: preference, decision, state, fact, lesson, procedure, or reference.
- source:
  - stated: Ted said it and committed to it.
  - observed: seen in a file, a command's output, a log, or an error.
  - inferred: your own conclusion from the session.
  - suggested: an idea, a question, or a tentative remark nobody committed to.
  A tentative remark from Ted ("I think I'll switch to X", "maybe we should") is suggested,
  not stated.
- quote: copy the words that support the fact exactly as they appear in one entry, at
  most 300 characters. Do not paraphrase or join words from different entries.
- entry: the id of the entry the quote comes from.
- scope: global only for Ted's preferences and environment that apply to every project;
  otherwise project.

Also write:
- summary: two to four sentences on what happened.
- handoff: what was in flight when this part ended, approaches that failed, and what is
  still open. Use empty values when there is nothing to hand off.

Never invent a fact, a quote, or an entry id.
