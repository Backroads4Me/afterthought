# Afterthought

Shared, evidence-backed memory for coding agents. [Claude Code](https://code.claude.com/docs)
and pi read it when a session starts and search it when they need more. A model you choose builds it from your
past sessions in the background, and nothing becomes memory unless a script can find the
quote it rests on in what was actually said.

## Why

Coding agents forget everything between sessions, and the memory features they do have
depend on the agent deciding to use them, which in practice it mostly does not.
Afterthought works from the agent's lifecycle hooks instead:

- **At session start** the agent is handed a short map of the project: what the last
  session left in flight, the decisions and facts that hold, your preferences, and where
  to look for more.
- **At session end** the conversation is filtered, archived, and summarized into a digest
  by an OpenAI-compatible model (a local vLLM server works well).
- **In the background** a "dream" step turns digests into curated facts, one decision at a
  time, under fixed rules. Every fact keeps a verbatim quote from its source, and you can
  trace any line back to the words it came from.

Memory is plain markdown in a git repository that doubles as an Obsidian vault, so you can
read it, edit it, and revert any change the tool made.

## Install

Afterthought needs Linux, Python 3.11 or later, [uv](https://docs.astral.sh/uv/), git, and an
OpenAI-compatible model endpoint. Background work runs from a systemd user timer
(`aft install timer`); elsewhere, run `aft run` from a scheduler of your own.

```bash
uv tool install git+https://github.com/Backroads4Me/afterthought
```

To work on Afterthought itself, install from a checkout instead, so the `aft` command runs
your working copy:

```bash
git clone https://github.com/Backroads4Me/afterthought && cd afterthought
uv tool install --editable .
```

## Quick start

1. Create the config file and the vault:

   ```bash
   aft init
   ```

2. Point Afterthought at your model. In `~/.afterthought/config.toml`, set `[model]`
   `endpoint` and `name`. See [configuration](docs/configuration.md).

3. Register the projects you work in. Any directory works, git repository or not:

   ```bash
   aft project add ~/src/my-project
   ```

4. Hook it into your agents. This adds the hooks that capture sessions and inject memory,
   and the `aft` skill, which lets you ask an agent to search memory, pause background
   work, import documentation and the rest in plain words. Each command has `--dry-run` to
   show the change first:

   ```bash
   aft install claude
   aft install pi
   ```

5. Turn on background work, which digests sessions and dreams every 30 minutes:

   ```bash
   aft install timer
   ```

6. Open `~/afterthought` as a vault in Obsidian to read and edit memory. When memory has a
   question for you, the next session tells you and offers to settle it with you;
   `reports/needs-you.md` lists every one.

`aft pause` stops background work at any time without losing anything, since sessions are
still queued, and `aft resume` picks them up.

Start a new session in a registered project and the agent's first context is its memory
block. `aft inject` prints the same block from the command line.

## How it works

A session's words pass through four stages, each with a guard. **Evidence** is the
filtered conversation, kept in a local archive with secrets redacted. A **digest** is a
model's reading of one session: a summary, a handoff for the next session, and proposed
facts, each with a quote the script verifies. **Dreaming** decides, fact by fact, whether
to add, confirm, replace or dispute a line of memory, and fixed rules decide whether that
decision may stand. What survives is **memory**: long-term facts that stay until something
replaces them, and short-term facts about the current work that roll off after a few
sessions. [Concepts](docs/concepts.md) explains each stage.

## Documentation

- [Concepts](docs/concepts.md): evidence, digests, facts, source tags, short-term and
  long-term memory, and what you will see day to day.
- [Configuration](docs/configuration.md): every setting in `config.toml`.
- [Commands](docs/commands.md): every `aft` command.
- [The vault](docs/vault.md): its layout, editing memory by hand, reports, reverting and
  forgetting.
- [Design](planning/DESIGN.md): why it works the way it does.

## Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
```

Set `AFT_HOME` to point `aft` at a scratch home directory; the test suite does this so it
never touches real memory.

## License

[MIT](LICENSE)
