You restate candidate facts that were refused because they used names, numbers, or key
terms that did not appear in their source text.

Each item gives:
- an item label in brackets, such as [#1]
- Statement: the refused statement
- Missing words: the words the statement named that the source text does not hold
- Source text: the only text the claim may be grounded in

For each item:
Rewrite the statement into one plain sentence saying the same claim, but writing every name,
number, and key term exactly as the source text writes it. Do not introduce any new names,
numbers, abbreviations, or external terms not present in the source text.

If the claim cannot be stated without the missing words or terms absent from the source text,
return an empty statement for that item.

Example 1:
Statement: The cache service runs on Redis 7.2.
Missing words: Redis, 7.2
Source text: cache layer is active on port 6379
Restated: The cache layer is active on port 6379.

Example 2:
Statement: The client library communicates via gRPC protocol.
Missing words: gRPC, protocol
Source text: client connects using json over http
Restated: The client connects using json over http.

Example 3:
Statement: Device firmware was upgraded to version 4.0.
Missing words: 4.0
Source text: device received an update yesterday
Restated: The device received an update yesterday.

Example 4:
Statement: The sensor transmits data via Bluetooth.
Missing words: Bluetooth
Source text: readings are logged to local storage
Restated: (leave statement empty: "")

For each item give:
- item: the item label, such as #1.
- statement: the restated sentence in the source text's words, or empty if it cannot be grounded.
