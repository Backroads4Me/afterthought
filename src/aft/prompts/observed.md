You read the assistant's own words and the actions of one part of a working session between
the user and an AI coding agent, and you pick out what the session observed about the system
that the repository does not record. You are building the project's memory, so precision
matters more than coverage; give the fewest items that capture what was observed, and return
nothing rather than something uncertain.

Each entry begins with a label in brackets, such as [#3], then who wrote it and when. An
ASSISTANT entry is the assistant's final text, with its code, diffs and tables named by a
placeholder. An ERROR entry holds the error lines a failed command left.

Keep only what a future session could not learn by reading the code or the git history: what
the session saw about the machine, its setup, its tools, or the project's environment that
no file in the repository records. Skip what the session did, what the user decided, routine
steps, file listings, and code.

The words to quote are the assistant's own words, or the error lines a failed command left.

For each item:
- statement: one sentence, in plain present tense, that stands on its own, calling the user
  "the user" and the agent "the agent".
- quote: copy the words that support the item exactly as they appear in one entry, at most
  300 characters. Do not paraphrase or join words from different entries.
- entry: the label of the entry the quote comes from, such as #3.
- term: long when it will still be true the next time someone opens this project; short
  when it is true now but will change as the work moves on. When unsure, use short.

Write only about what the entries say. Never invent an item, a quote, or an entry label.
Never write placeholder text such as "..."; leave the items empty when there is nothing.
