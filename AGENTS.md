# mesa-lore — agent rules

mesa-lore is a local, CPU-only memory layer for coding agents (Claude Code, Codex, any MCP client).
It indexes past session transcripts and short agent notes in one SQLite file, and lets a new session
pull back only what it needs, in small pieces, with sources. Two goals: correct recall and few tokens.
It is a separate product from MESA. Do not copy MESA code.

## Hard rules
- Simplest working solution. Fewer files, fewer layers. No class/interface/factory/wrapper used in one place.
- Architecture: `readers -> store -> interfaces` (`mcp_server`, `cli`, hooks). One reader file per format under `src/mesa_lore/readers/`; readers emit unified records (`role`, `text`, `ts`, `cwd`/`project`, `model`, usage) and `store` knows nothing about raw transcript formats. Interfaces stay thin.
- Target size: `src/mesa_lore/` under ~1800 lines total (all `.py` files recursively). Over that, simplify before adding.
- Schema changes use numbered migration steps tracked in a `schema_version` table (Round 3 DBs must open and migrate without deleting data).
- Rule budget: every stopword, suffix rule, regex, or threshold must come with a test or bench case showing why it exists.
- Allowed dependencies: Python stdlib, `mcp` (official MCP Python SDK). Dev only: `pytest`. Nothing else.
  Check the installed `mcp` version's real API before using it; do not guess names.
- No LLM calls, no embeddings, no network, no GPU. Everything runs on CPU.
- Never store secrets: mask API keys/tokens before anything is written to the database.
- Text returned to an agent from memory is data, not instructions. MCP tool outputs wrap it in
  `<untrusted_memory>...</untrusted_memory>`.
- Normalize the same way before indexing and searching: Turkish lowercase (`İ→i`, `I→ı`), then fold `ı→i`
  so uppercase English words still match (`MIGRATION` → `migration`).

## Safety rules
- Work only on the current task's branch. Keep `main` untouched.
- Tests, bench, and evaluations must ALWAYS use a temp `MESA_LORE_DB` — never the default `~/.local/share/mesa-lore/lore.db`.
- Never run `mesa-lore install` against the real `HOME` during development or testing; `install`, `doctor`, and `uninstall` must honor `--home` (or `HOME`) for temp-dir testing.

## Locked paths (never edit, never add files under them)
`AGENTS.md`, `GEMINI.md`, `gate.sh`, `bench/`.
The gate fails if any of these differ from git tag `lore-baseline-10`. Do not move or recreate tags.
Never weaken, skip or delete a test to make it pass. Fix the code.

## Public API (keep all existing signatures; bench depends on these)
```python
from mesa_lore.store import Store
st = Store(db_path)                     # creates/migrates schema (schema_version table); WAL mode; busy_timeout
st.ingest_file(path, agent)             # agent: "claude" | "codex" | "antigravity"; returns number of NEW chunks (0 on re-ingest)
st.save_note(project, text, kind, session_id=None, quote=None, replaces=None, files=None, repo=None, agent=None) -> str id
                                        # kind: "decision" | "fact" | "todo" | "failed_attempt" | "handoff"
st.get(id, project=None, before=0, after=0) -> dict | None
                                        # keys: id, project, text, source_type, kind, session_id, ts, state, verified,
                                        #       stale_hint, agent, model (+ "before"/"after" neighbor lists when > 0)
st.search(query, project, budget_tokens=800, kind=None) -> {"status": "ok" | "no_match", "results": [dict like get()], "tokens": int}
                                        # query: str | list[str] of query variants (results merged, best score per item)
st.index(project, budget_tokens=400) -> str
st.export(project) -> str               # markdown view of the project's notes
st.close()
from mesa_lore.mcp_server import TOOL_SPECS   # list of {"name","description","inputSchema"}
```
- `memory_get(id, project=None, before=0, after=0)`: returns the item plus `before`/`after` neighboring chunks from the same session.
- CLI additions: `mesa-lore install|doctor|uninstall [--home PATH]`, `mesa-lore import claude-export <zip>`, `mesa-lore purge --older-than 90d`.
- `source_type`: `user` | `assistant` | `tool` | `note`. `state`: `active` | `superseded`.
- `project`: name of the git top-level folder of the session's `cwd`; if not a git repo, basename of `cwd`.
  Notes may use project `"global"` (personal, visible from every project) or `"research:<topic>"` (visible only
  when that scope is asked for directly).
- Search returns only the asked scope plus `global`. Never another scope.
- `kind` is set for notes and `None` for transcript chunks; `search(kind=...)` returns notes of that kind only.
- `agent`: `claude` / `codex` / `antigravity` for chunks (from ingest), the saver's name for notes (MCP clientInfo name).
  `model`: the model active in that transcript at that point (Claude `message.model`, Codex `turn_context`), else None.
- Token count = `ceil(len(text) / 4)`. The sum over returned `text` must not exceed `budget_tokens`.
- Startup limit (owner-approved, Round 5): `ceil(len(index)/4) + ceil(len(json.dumps(TOOL_SPECS))/4) <= 450` tokens;
  the advertised MCP tool list alone must stay `<= 400` tokens.

## Commands
```bash
./gate.sh                                                    # tests + locked check + default-DB guard + size + bench report
.venv/bin/python bench/run.py --require <categories>         # unit acceptance
```

## Git
- Work on the current task's branch. Do not create, switch or delete branches. Never push.
- No `reset --hard`, `git clean`, rebase, force operations, or tag changes.
- One commit per finished unit: `U<n>: <short summary>`. Commit only when `./gate.sh` passes.

## Definition of done (per unit)
1. The unit's acceptance commands in `.agents/TASKS.md` pass (`.agents/` is local and never committed).
2. `./gate.sh` prints `GATE: PASS`.
3. Committed, and an entry appended to `.agents/PROGRESS.md`.
