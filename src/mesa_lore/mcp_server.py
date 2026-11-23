import asyncio, os
from contextlib import closing
from mcp.server.mcpserver import Context, MCPServer
from mesa_lore.store import Store, get_db_path, get_project, scan_antigravity
from mesa_lore.text import wrap_untrusted

_S, _I = {"type": "string"}, {"type": "integer"}
TOOL_SPECS = [
    {"name": "memory_index", "description": "Project memory overview.", "inputSchema": {"type": "object", "properties": {"project": _S}}},
    {"name": "memory_search", "description": "Search (2-5 keywords; synonyms or English).", "inputSchema": {"type": "object", "properties": {"query": {}, "project": _S, "kind": _S}, "required": ["query"]}},
    {"name": "memory_get", "description": "Get entry + before/after neighbors.", "inputSchema": {"type": "object", "properties": {"id": _S, "project": _S, "before": _I, "after": _I}, "required": ["id"]}},
    {"name": "memory_save", "description": "Save note (kind: decision|fact|todo|failed_attempt|handoff).", "inputSchema": {"type": "object", "properties": {"text": _S, "kind": _S, "project": _S, "session_id": _S, "replaces": _S, "files": {"type": "array", "items": _S}, "repo": _S}, "required": ["text", "kind"]}},
]

def _run(project, fn) -> str:
    """Open the store, run fn(store, project) and wrap whatever comes back, errors included."""
    try:
        with closing(Store(get_db_path())) as st:
            return wrap_untrusted(fn(st, project or get_project(os.getcwd())))
    except Exception as e:  # noqa: BLE001 - an MCP client should always get wrapped text
        return wrap_untrusted(f"Error: {type(e).__name__}: {e}")

app = MCPServer("mesa-lore")

@app.tool(name="memory_index", structured_output=False)
def memory_index(project: str | None = None, budget_tokens: int = 400) -> str:
    return _run(project, lambda st, p: st.index(p, budget_tokens=budget_tokens) or f"No memory index for project: {p}")

@app.tool(name="memory_search", structured_output=False)
def memory_search(query: str | list[str] = "", project: str | None = None, budget_tokens: int = 800, kind: str | None = None) -> str:
    if not query or (isinstance(query, str) and not query.strip()):
        return wrap_untrusted("Error: query is required.")

    def search(st, p):
        r = st.search(query, p, budget_tokens=budget_tokens, kind=kind)
        if not r["results"]:
            return "No matching memory found."
        lines = []
        for i in r["results"]:
            parts = [x for x in (i.get("agent"), i.get("model"), (i.get("ts") or "")[:10]) if x]
            tag = f" [{'·'.join(parts)}]" if parts else ""
            stale = " [STALE]" if i.get("stale_hint") else ""
            info = f"note·{i.get('kind')}·{i.get('state')}" if i.get("source_type") == "note" else i.get("source_type", "")
            lines.append(f"- [{i['id']}]{tag} ({info}{stale}): {i['text']}")
        return "\n".join(lines)
    return _run(project, search)

@app.tool(name="memory_get", structured_output=False)
def memory_get(id: str = "", project: str | None = None, before: int = 0, after: int = 0) -> str:
    if not id or not id.strip():
        return wrap_untrusted("Error: id is required.")

    def get(st, p):
        item = st.get(id, project=p, before=before, after=after)
        if not item:
            return f"Item not found: {id}"
        out = [f"{k}: {item[k]}" for k in ("id", "project", "source_type", "state", "verified", "stale_hint", "agent", "model", "text")]
        for label in ("before", "after"):
            if item.get(label):
                out.append(f"{label}:")
                out.extend(f"  - [{x['id']}] ({x['source_type']}): {x['text']}" for x in item[label])
        return "\n".join(out)
    return _run(project, get)

@app.tool(name="memory_save", structured_output=False)
def memory_save(text: str = "", kind: str = "", project: str | None = None, session_id: str | None = None,
                quote: str | None = None, replaces: str | None = None, files: list[str] | None = None,
                repo: str | None = None, ctx: Context | None = None) -> str:
    if not text or not text.strip() or kind not in ("decision", "fact", "todo", "failed_attempt", "handoff"):
        return wrap_untrusted("Error: valid text and kind (decision|fact|todo|failed_attempt|handoff) are required.")
    agent = None
    if ctx:
        try:
            cp = ctx.session.client_params
            agent = cp.client_info.name if cp and cp.client_info else None
        except Exception:
            pass

    def save(st, p):
        hints = [] if replaces else st.replace_hints(p, text, kind)
        nid = st.save_note(p, text, kind, session_id=session_id, quote=quote, replaces=replaces, files=files, repo=repo, agent=agent)
        hint = f"\npossibly replaces: {', '.join(hints)} (if so, save the same text again with replaces=<id>)" if hints else ""
        return f"Saved note: {nid} (kind: {kind}, project: {p}){hint}"
    return _run(project, save)

for _s in TOOL_SPECS:  # clients see the compact TOOL_SPECS, not the larger SDK-generated schemas
    _t = app._tool_manager.get_tool(_s["name"])
    _t.parameters, _t.description = _s["inputSchema"], _s["description"]

def run_server():
    try:
        with closing(Store(get_db_path())) as st:
            scan_antigravity(st)
    except Exception:
        pass
    asyncio.run(app.run_stdio_async())

if __name__ == "__main__":
    run_server()
