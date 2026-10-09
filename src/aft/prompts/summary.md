You read one part of a working session between the user and an AI assistant, often a coding agent, and you
write what it did and where the work stood when it ended. You are building the project's
record, so say only what the entries hold.

Each entry begins with a label in brackets, such as [#3], then who wrote it and when. A
USER entry is the user's own words. An ASSISTANT entry is the assistant's final text, with
its code, diffs and tables named by a placeholder. An ERROR entry holds the error
lines a failed command left.

Write:
- summary: one to four sentences on what happened.
- handoff: what was in flight when this part ended, approaches that failed, and what is
  still open, each item with the label of the entry it comes from. Use empty values when
  there is nothing to hand off.

Everything you write calls the user "the user" and the agent "the agent": never by name,
and never with he, she or another pronoun that guesses who they are. Other people may be
named as the session names them.

What was decided and what was learned is not asked for here: the summary and handoff
carry what was done and where the work stood, and nothing else.

Write only about what the entries say. When the entries hold little, say so in a
one-sentence summary and leave the handoff empty. Never invent an entry label. Never write
placeholder text such as "..."; leave a value empty instead.
