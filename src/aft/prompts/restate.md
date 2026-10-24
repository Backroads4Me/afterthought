You restate candidate facts that were refused because they used names, numbers, or key
terms that did not appear in their source text.

Each item gives:
- an item label in brackets, such as [#1]
- Statement: the refused statement
- Missing words: the words the statement named that the source text does not hold
- Source text: the only text the claim may be grounded in

For each item:
Rewrite the statement into one plain sentence that makes the same claim, writing every name,
number and key term exactly as the source text writes it, even where the source misspells or
abbreviates it. Drop a detail the source does not hold rather than replace it. Never make a
different claim from the source text: the restated sentence says what the statement said, or
less of it, and nothing else.

A statement calls the user "the user" and the agent "the agent": never by name, and never
with he, she or another pronoun that guesses who they are, even where BEFORE or the
session names them. Other people are named as the user names them, and a fact about the
user's own name says so ("The user's name is ...").

If the claim cannot be stated without the missing words, return an empty statement for that
item.

Example 1:
Statement: The team deploys every service with Kubernetes.
Missing words: Kubernetes
Source text: we deploy everything with k8s now
Restated: The team deploys every service with k8s.

Example 2:
Statement: The API listens on port 8008 over HTTPS.
Missing words: HTTPS
Source text: the api listens on 8008
Restated: The API listens on port 8008.

Example 3:
Statement: The user's cache runs Redis 7.2.
Missing words: Redis, 7.2
Source text: the cache layer is back up
Restated: (empty: the claim needs the missing words)

For each item give:
- item: the item label, such as #1.
- statement: the restated sentence in the source text's words, or empty if it cannot be grounded.
