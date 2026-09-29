# afterthought

Shared, evidence-backed memory for coding agents. Claude Code and pi read it at session
start and search it on demand; a local model builds it from past sessions, and nothing
enters memory without a quote a script can find in its source.

The design is in [planning/DESIGN.md](planning/DESIGN.md) and the build plan in
[planning/BUILD-PLAN.md](planning/BUILD-PLAN.md).

## Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run aft --help
```

`aft` reads `~/.afterthought/config.toml`. Set `AFT_HOME` to point it at another home
directory; the test suite does this so it never touches real memory.

## Commands

| Command | Does |
|---|---|
| `aft init` | Write a default `config.toml` if there is none, and create the vault |
| `aft key [path]` | Show which project a directory belongs to; exits 1 when it is not registered |
| `aft inject [path]` | Print the memory a session starts with; reads the vault's files only |
| `aft project list\|add\|rename\|forget` | Manage the project registry |
| `aft lint` | Check every page against OKF v0.2 and the fact-line grammar |
| `aft work` | Sweep for sessions no hook reported, then digest the queue within the day's token budget; exits 1 when a job could not be digested, and 0 when the budget simply ran out |
| `aft install pi [--dry-run\|--uninstall]` | Copy this checkout's extension into pi's extensions directory, preview it, or remove it again |
| `aft reindex` | Drop the search index and rebuild it from the vault's files and the evidence archive, naming any page it could not read |
