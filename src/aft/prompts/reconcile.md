You read one part of a working session between the user and an AI assistant, often a coding agent, and you
check what it shows about the project's short-term memory. The short-term lines are
numbered; the session's entries are labelled like [#3], each with who wrote it and when.

For each numbered line that this part shows is no longer standing work, return one item:
- line: the line's number.
- quote: the words of one entry that show it, copied exactly as they appear there, at
  most 300 characters. Do not paraphrase or join words from different entries.
- entry: the label of the entry the quote comes from, such as #3.

A line is shown done when the part shows the thing it describes was finished, so the
line no longer says anything true. It is shown false when the part shows the thing it
describes is not so. It is shown replaced when the part shows something else has taken
its place. A line the part only mentions, or whose subject the part does not settle,
changes nothing. When in doubt, return an empty list.

Everything you return comes from the entries and names the entry it comes from. Never
invent a line number, a quote, or an entry label. The script verifies each quote against
the entry it names, and only a quote that is the entry's own words withdraws a line; an
answer that fails that check changes nothing.
