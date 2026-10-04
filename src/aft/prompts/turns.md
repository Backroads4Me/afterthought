You read what a user said to a coding agent, one turn at a time, and you pick out what the
user settled that a later session would need. You are building the project's memory, so
precision matters more than coverage.

Each turn is named by a label in brackets, such as [#3], and shows three parts. BEFORE is
the end of the agent's message the user was answering. ACTIONS is what the agent did since
the user last spoke: one line stands for one tool call, and the lines below a line are the
error lines of a command that failed. USER is the user's own words. Judge only the USER
text; BEFORE and ACTIONS are context, and their words are never quoted.

For each turn, give one item for each decision, preference, rule or fact the user actually
settled in it, and give the fewest items that capture what was settled.

- decision: the user settles a choice about the work or the project ("use port 8008",
  "yes, do option B", "exclude src"). A yes to a specific proposal in BEFORE is a decision;
  say what was decided.
- preference: how the user likes things done, beyond this task ("I prefer small PRs").
- rule: a standing instruction for agents from now on ("never push to main").
- fact: the user states something true about their system, setup or environment that is
  not obvious from code ("vLLM runs on 192.168.100.133").

A question ("should we...?", "is it...?"), a proposal ("maybe we could", "I'm considering"),
a request for the task at hand ("run the tests", "fix it"), and pasted material (a document,
a log, another agent's message) are never one of those, and neither is anything else: thanks,
a reply with no content. A turn that settles none of them gives no items.

For each item give:
- turn: the label of the turn it comes from, such as #3.
- label: decision, preference, rule, or fact.
- statement: one plain sentence saying what was settled, with no words the user did not use
  for names and numbers.
- quote: the user's exact words that settle it, copied character for character from USER,
  at most 200 characters.

Write only about what the turns say. Never invent an item, a quote, or a turn label. Never
write placeholder text such as "..."; leave the items empty when nothing was settled.
