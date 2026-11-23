import json
import math
from mesa_lore.mcp_server import TOOL_SPECS
from mesa_lore.store import Store

_tok = lambda s: math.ceil(len(s) / 4)
LONG = "Use SQLite WAL mode for the local store because concurrent hooks write while the MCP server reads it"


def _handoff(st, i, text):
    st.con.execute("INSERT INTO handoffs (id, project, text, session_id, ts, state, agent) VALUES (?, 'app', ?, ?, ?, 'active', 'claude')",
                   (f"h_{i}", text, f"s{i}", f"2026-11-1{i}T10:00:00Z"))
    st.con.commit()


def test_index_readable_note_lines_and_handoff_titles(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        for i in range(4):
            st.save_note("app", f"{LONG} ({i})", "decision")
        _handoff(st, 1, "İstek: x\nYapılan: 3. **Not Yazma** ve `memory_save` güvenliği tamamlandı ve testler yeşil")
        idx = st.index("app")
        note_lines = [ln for ln in idx.splitlines() if ln.startswith("- [decision]")]
        assert len(note_lines) == 3  # was 2
        assert "concurrent hooks write" in note_lines[0]  # was clipped at ~50 chars
        ho = next(ln for ln in idx.splitlines() if ln.startswith("- 2026-11-11"))
        assert ho == "- 2026-11-11: Not Yazma ve memory_save güvenliği tamamlandı ve testler yeşil"
    finally:
        st.close()


def test_startup_worst_case_stays_under_owner_limit(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        st.save_note("global", "g " + LONG * 3, "failed_attempt")
        for i in range(5):
            st.save_note("app", LONG * 3, "failed_attempt")
        for i in range(1, 5):
            _handoff(st, i, "Yapılan: " + LONG * 3)
        st.save_note("some-other-project-with-a-long-name", "x", "fact")
        idx = st.index("app")
        tools = json.dumps(TOOL_SPECS, ensure_ascii=False)
        assert _tok(tools) <= 400
        assert _tok(idx) + _tok(tools) <= 450, (_tok(idx), _tok(tools))
    finally:
        st.close()


def test_tool_specs_expose_save_fields_and_kinds():
    save = next(t for t in TOOL_SPECS if t["name"] == "memory_save")
    get = next(t for t in TOOL_SPECS if t["name"] == "memory_get")
    assert {"replaces", "files", "repo"} <= set(save["inputSchema"]["properties"])
    assert "project" in get["inputSchema"]["properties"]
    assert all(k in save["description"] for k in ("decision", "fact", "todo", "failed_attempt", "handoff"))


def test_mcp_save_replaces_and_files_reach_the_store(tmp_path, monkeypatch):
    import subprocess
    from mesa_lore import mcp_server
    repo = tmp_path / "r"
    repo.mkdir()
    for cmd in (["git", "init", "-q"], ["git", "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(cmd, cwd=repo, check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-q", "-m", "a"], cwd=repo, check=True)
    monkeypatch.setenv("MESA_LORE_DB", str(tmp_path / "m.db"))
    old = mcp_server.memory_save("Use Redis for the cache layer", kind="decision", project="app").split("Saved note: ")[1].split()[0]
    mcp_server.memory_save("Use Dragonfly for the cache layer", kind="decision", project="app", replaces=old, files=["a.py"], repo=str(repo))
    st = Store(tmp_path / "m.db")
    try:
        assert st.get(old)["state"] == "superseded"
        row = st.con.execute("SELECT files, repo FROM entries WHERE replaces = ?", (old,)).fetchone()
        assert json.loads(row["files"]) == ["a.py"] and row["repo"] == str(repo)
    finally:
        st.close()


def test_git_staleness_checked_once_per_file_per_call(tmp_path, monkeypatch):
    import subprocess
    from mesa_lore import store as store_mod
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    for cmd in (["git", "init", "-q"], ["git", "add", "a.py"], ["git", "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-q", "-m", "i"]):
        subprocess.run(cmd, cwd=repo, check=True)
    st = Store(tmp_path / "lore.db")
    try:
        for i in range(5):
            st.save_note("myrepo", f"Cache rule {i} for parser token_{i}", "decision", files=["a.py"], repo=str(repo))
        calls, orig = [], store_mod.subprocess.run
        monkeypatch.setattr(store_mod.subprocess, "run", lambda cmd, *a, **kw: (calls.append(cmd) if cmd[:2] == ["git", "log"] else None) or orig(cmd, *a, **kw))
        assert len(st.list_entries("myrepo")) == 5 and len(calls) == 1
        calls.clear()
        assert len(st.search("cache rule parser", "myrepo")["results"]) == 5 and len(calls) == 1
        # the cache is per call: a commit touching a.py is still seen by the next call
        (repo / "a.py").write_text("x = 2\n")
        subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-q", "-am", "c"], cwd=repo, check=True)
        assert all(e["stale_hint"] for e in st.list_entries("myrepo"))
    finally:
        st.close()


def test_doc_counts_cached_across_query_variants(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        st.save_note("app", "Parser cache uses an LRU of 128 entries", "decision")
        counts = []
        st.con.set_trace_callback(lambda sql: counts.append(sql) if sql.startswith("SELECT count(*) FROM entries_fts WHERE") else None)
        st.search(["parser cache", "parser cache size", "cache parser"], "app")
        assert len(counts) == 3  # parser, cache, size: each counted once
    finally:
        st.close()
