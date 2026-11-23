# mesa-lore

mesa-lore is a local, CPU-only memory layer for coding agents. It indexes past session transcripts and short agent
notes in one SQLite file, then lets a new session pull back only what it needs, in small pieces, with sources. It makes
no LLM calls, uses no embeddings, and needs no network at runtime.

**Website:** [mesamemory.dev/lore](https://mesamemory.dev/lore/) · [Türkçe](https://mesamemory.dev/tr/lore/)

## Why

Resuming work usually means replaying a long session or explaining the context again, and both cost tokens. mesa-lore
starts a session with a short index and lets the agent search for details only when it needs them.

## Install

Requires Python 3.10 or newer.

```bash
git clone https://github.com/Yasou13/MESA_Lore.git
cd MESA_Lore
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/mesa-lore install
.venv/bin/mesa-lore doctor
```

`install` adds the MCP server and hooks to the supported agents and imports existing history. It backs up files it
changes to `~/.config/mesa-lore/backup/`.

```bash
mesa-lore install [--home PATH]    # configure agents and import history
mesa-lore doctor [--home PATH]     # check configuration, schema and ingest warnings
mesa-lore uninstall [--home PATH]  # restore backed-up configs and keep the database
```

## Supported agents

| Agent | Memory tools | Automatic capture |
|---|---|---|
| Claude Code | MCP | `SessionStart`, `Stop` and `SessionEnd` hooks |
| Codex CLI | MCP | Same hooks, after you approve them with `/hooks` in Codex |
| Antigravity | MCP | Transcripts are scanned when the server starts or on `ingest-pending` |
| Claude desktop | MCP | None. Chats on claude.ai and in the desktop chat tab are not captured |

## CLI

```bash
mesa-lore search "why did login fail" --project my-app
mesa-lore index --project my-app
mesa-lore show <id> --before 2 --after 2
mesa-lore export --project my-app --out my-app.md
mesa-lore export --all -o notes/
mesa-lore stats --days 30
mesa-lore purge --older-than 90d
```

Run `mesa-lore --help` for all commands.

## Memory and handoffs

Agents use four MCP tools: `memory_index`, `memory_search`, `memory_get` and `memory_save`.

`memory_save` stores a short note of kind `decision`, `fact`, `todo`, `failed_attempt` or `handoff`. A note can replace
an older one, which marks the older note as superseded.

A handoff tells the next session what was done, what was decided and what comes next. mesa-lore builds a basic handoff
from each ingested session. In agents with a `Stop` hook, it also asks the agent to write its own at turn 8, then at
most once every 20 turns. An agent-written handoff takes precedence.

`mesa-lore export` writes a Markdown notebook for a project: open items, decisions, then up to 30 recent sessions
grouped by date and agent. `export --all -o notes/` writes one file per project.

## Project scopes

Each session is stored under the Git repository it worked in, and worktrees of one repository share a scope. A session
started outside a repository is assigned from the files it touched, and files it edited count before files it only
read. A session with no repository goes to `misc`.

Search returns the current project plus `global` notes. `research:<topic>` scopes are returned only when asked for
directly.

## Data and privacy

- Data stays in `~/.local/share/mesa-lore/lore.db`. Set `MESA_LORE_DB` to use another path.
- Nothing is sent over the network by mesa-lore.
- Common API keys, tokens, passwords and private keys are masked before storage. Masking is pattern-based, so an
  unusual secret format can be missed.
- Text returned to agents is wrapped in `<untrusted_memory>` tags so it is treated as data.
- To exclude projects from ingest, add this to `~/.config/mesa-lore/config.toml`:

```toml
excluded_projects = ["private-client"]
```

## Platform status

- Linux: tested. CI runs the full suite on Python 3.10 to 3.14.
- macOS: CI runs the full suite on Python 3.14. Not tested by hand.
- Windows: experimental. CI runs the portability, notebook, wrapper and isolation tests on Python 3.14, but not the
  full suite or benchmark.

## Measured results

From the included test suite and benchmark on Linux with Python 3.14:

- 218 tests pass and all 31 required benchmark categories pass.
- Keyword search finds 8 of 9 benchmark questions. Turkish search finds 3 of 3.
- Session startup costs 432 tokens: 163 for the index and 269 for tool definitions.
- Search p95 was 84 ms of CPU time on 10,000 chunks in one benchmark run.

From a private evaluation on the author's own transcripts (38,291 entries, 168 MB), which cannot be reproduced from this
repository:

- 51.7% of 120 questions were answered correctly with an 800-token memory budget. Answers and grading used LLMs.
- The `Stop` hook has a median runtime of 37.5 ms.

End-to-end token savings when resuming a session have not been measured yet.

## Limitations

- Search is lexical. It has no embeddings or synonym understanding, so queries need words or stems present in the text.
- Recall is incomplete, so check important answers against the returned source.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
MESA_LORE_DB="$(mktemp -d)/lore.db" .venv/bin/python -m pytest -q
MESA_LORE_DB="$(mktemp -d)/lore.db" .venv/bin/python bench/run.py --require all
```

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
