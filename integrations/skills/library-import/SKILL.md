---
name: library-import
description: Import reference documentation and collections into the afterthought library. Use when asked to import or add reference documents, specifications, or documentation collections into afterthought (e.g. "add my RV-C documentation to the library"). Drives aft library survey, validate, and import commands.
---

# Library Import

Import reference documentation and specification collections into the afterthought vault.
The agent never writes directly into the library itself; deterministic `aft library` commands check and carry out the plan.

## Step-by-Step Procedure

1. **Survey the collection**:
   Run `aft library survey <path>` to inspect the collection directory.
   This changes nothing. It lists every file with its type, size, and SHA-256 hash, flags duplicates, suggests likely derived pairs (the same name with a different extension), identifies instruction files and tooling that must be excluded, and displays the import procedure.

2. **Write a manifest**:
   Create a YAML manifest file (e.g. `<collection>.yaml`) defining:
   - `name`: collection slug (lowercase, dashes/underscores)
   - `title`: human-readable title
   - `description`: collection summary
   - `projects`: list of project slugs this collection applies to
   - `files`: mapping of every surveyed relative file path to its role and relations

   **File Roles** (every surveyed file must have exactly one role):
   - `original`: authoritative source document
   - `derived`: version made from another file (must record `derived-from` relation)
   - `errata`: corrections to another document (must record `errata-for` relation)
   - `notes`: working notes kept as sources and mined for candidate facts
   - `exclude`: not imported (build scripts, generated indexes, git history, AGENTS.md, tooling)

   **Relations**:
   - `derived-from`: points to the original file in the collection
   - `errata-for`: points to the document being corrected
   - `supersedes`: points to an older edition being replaced

3. **Validate the manifest**:
   Run `aft library validate <manifest>`.
   Validates before touching anything:
   - Every surveyed file has exactly one role.
   - Every relation points to a file within the collection.
   - Instruction files and tooling are marked `exclude`.

4. **Import the collection**:
   Run `aft library import <manifest>`.
   This command:
   - Validates the manifest.
   - Copies non-excluded files to `library/<name>/` as read-only.
   - Extracts text by content hash into `~/.afterthought/library/`.
   - Indexes documents in the `library` search collection.
   - Verifies claims from existing map files (e.g. `index.json`, README tables) as evidence.
   - Generates reference pages under `references/<name>/`.
   - Writes an import report and commits to the vault.

5. **Add descriptions**:
   Review generated reference pages. Add descriptions explaining what each document is, its authority, and when to use or not use it. Every claim must carry a quote that appears in the extracted text.

6. **Propose facts**:
   Propose candidate facts mined from notes and excluded instruction files. Each fact is quote-checked against its file and enters the dream as a candidate. Excluded files record that the quote was verified at import and the original was not retained.

7. **Review report and commit**:
   Review the import report and vault commit to verify all files are accounted for and all claims verified.
