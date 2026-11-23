import json
from mesa_lore.store import Store


def test_two_stage_search_prevents_tool_flood_starvation(tmp_path):
    tf = tmp_path / "flood.jsonl"
    lines = []
    # 220 high-TF tool outputs that saturate raw LIMIT 200 BM25 pool
    for i in range(220):
        lines.append(json.dumps({
            "type": "user", "sessionId": f"s_tool_{i % 3}", "cwd": str(tmp_path),
            "timestamp": f"2026-11-10T10:00:{i % 60:02d}Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tu{i}", "content": ("telemetry_quota_limit " * 6) + f"noise_{i}"}]},
        }))
    # 1 concise assistant conclusion that would miss a shared BM25 LIMIT 200
    lines.append(json.dumps({
        "type": "assistant", "sessionId": "s_asst", "cwd": str(tmp_path),
        "timestamp": "2026-11-10T11:00:00Z",
        "message": {"role": "assistant", "content": "Set telemetry_quota_limit to 4200 for staging."},
    }))
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(tf, "claude")
        res = st.search("telemetry_quota_limit", tmp_path.name, budget_tokens=500)
        assert res["status"] == "ok"
        assert any("4200" in r["text"] for r in res["results"]), f"assistant starved by tool flood: {res['results']}"
        assert res["results"][0]["source_type"] != "tool"
        # First group fills first; tool chunks only follow after non-tool hits
        types = [r["source_type"] for r in res["results"]]
        if "tool" in types:
            first_tool = types.index("tool")
            assert all(t == "tool" for t in types[first_tool:])
    finally:
        st.close()


def test_handoff_fts_no_penalty_yapilan_index_and_clean_files(tmp_path):
    tf = tmp_path / "sess.jsonl"
    lines = [
        {
            "type": "user", "sessionId": "s_ho2", "cwd": str(tmp_path), "timestamp": "2026-11-12T10:00:00Z",
            "message": {"role": "user", "content": "Read AGENTS.md and continue refactoring carefully"},
        },
        {
            "type": "assistant", "sessionId": "s_ho2", "cwd": str(tmp_path), "timestamp": "2026-11-12T10:00:05Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "apply_patch", "input": "*** Update File: work/build_simple_report.py\\n@@\\n-old_line\\n+from pathlib import Path"}
            ]},
        },
        {
            "type": "user", "sessionId": "s_ho2", "cwd": str(tmp_path), "timestamp": "2026-11-12T10:00:06Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        },
        {
            "type": "assistant", "sessionId": "s_ho2", "cwd": str(tmp_path), "timestamp": "2026-11-12T10:00:10Z",
            "message": {"role": "assistant", "content": "Completed handoff pipeline cleanup and verified report export passes"},
        },
    ]
    tf.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(tf, "claude")
        proj = tmp_path.name
        h = next(e for e in st.list_entries(proj) if e["kind"] == "handoff")
        # 1) Indexed in entries_fts
        fts_row = st.con.execute("SELECT id FROM entries_fts WHERE id = ?", (h["id"],)).fetchone()
        assert fts_row is not None
        # 2) Clean files line without diff hunk markers
        files_ln = next(ln for ln in h["text"].splitlines() if ln.startswith("Files:"))
        assert "build_simple_report.py" in files_ln
        assert "@@" not in files_ln and "+from" not in files_ln and "-old_line" not in files_ln and "\\n" not in files_ln
        # 3) Index shows Yapilan snippet (not Istek boilerplate)
        idx = st.index(proj)
        assert "Completed handoff" in idx or "handoff pipeline" in idx
        assert "Read AGENTS.md" not in idx
        # 4) No -6.0 penalty: handoff matches alongside or above tool noise without kind filter
        res = st.search("Completed handoff pipeline cleanup", proj, budget_tokens=800)
        assert any(r["id"] == h["id"] for r in res["results"])
        # 5) Startup (TOOL_SPECS + index) <= 450 tokens
        import math
        from mesa_lore.mcp_server import TOOL_SPECS
        specs_tok = math.ceil(len(json.dumps(TOOL_SPECS, ensure_ascii=False)) / 4)
        idx_tok = math.ceil(len(idx) / 4)
        assert specs_tok + idx_tok <= 450, f"startup too large: {specs_tok} + {idx_tok}"
        # 6) Purge clears handoff FTS row as well
        st.purge(proj)
        assert st.con.execute("SELECT count(*) FROM entries_fts WHERE id = ?", (h["id"],)).fetchone()[0] == 0
    finally:
        st.close()


def test_v5_migrates_handoffs_into_fts(tmp_path):
    import sqlite3
    db = tmp_path / "v5.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE entries (id TEXT PRIMARY KEY, project TEXT NOT NULL, text TEXT NOT NULL, source_type TEXT NOT NULL,
            session_id TEXT, ts TEXT, state TEXT NOT NULL DEFAULT 'active', verified INTEGER, stale_hint INTEGER DEFAULT 0,
            kind TEXT, quote TEXT, replaces TEXT, files TEXT, repo TEXT, file_hashes TEXT, dedupe_key TEXT UNIQUE, agent TEXT, model TEXT);
        CREATE VIRTUAL TABLE entries_fts USING fts5(id UNINDEXED, text_norm, tokenize='trigram');
        CREATE TABLE usage (session_id TEXT NOT NULL, agent TEXT NOT NULL, project TEXT NOT NULL, ts TEXT,
            uncached_input INTEGER NOT NULL, cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL,
            output INTEGER NOT NULL, memory_used INTEGER NOT NULL DEFAULT 0, handoff_used INTEGER NOT NULL DEFAULT 0, dedupe_key TEXT UNIQUE);
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE ingest_warnings (path TEXT NOT NULL, agent TEXT NOT NULL, reason TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY (path, agent, reason));
        CREATE TABLE ingested_files (path TEXT NOT NULL, agent TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, PRIMARY KEY (path, agent));
        CREATE TABLE handoffs (id TEXT PRIMARY KEY, project TEXT NOT NULL, text TEXT NOT NULL, session_id TEXT NOT NULL, ts TEXT, state TEXT NOT NULL DEFAULT 'active', agent TEXT, UNIQUE(project, session_id));
        INSERT INTO handoffs VALUES ('h_v5_1', 'projx', 'İstek: abc\nYapılan: legacy handoff migration keyword', 's1', '2026-11-10T10:00:00Z', 'active', 'claude');
        INSERT INTO schema_version VALUES (5, '2026-11-10T00:00:00Z');
    """)
    con.commit()
    con.close()

    st = Store(db)
    try:
        assert st.con.execute("SELECT id FROM entries_fts WHERE id = 'h_v5_1'").fetchone() is not None
        res = st.search("legacy handoff migration keyword", "projx", budget_tokens=400)
        assert any(r["id"] == "h_v5_1" for r in res["results"])
    finally:
        st.close()


def test_search_fills_budget_past_five_and_keeps_session_cap(tmp_path):
    from collections import Counter
    db = tmp_path / "cap.db"
    st = Store(db)
    try:
        for i in range(8):
            st.save_note("proj", f"Note {i}: canary_budget_marker setting detail {i}.", "fact")
        res = st.search("canary_budget_marker", "proj", budget_tokens=800)
        assert res["status"] == "ok"
        assert len(res["results"]) == 8
        assert res["tokens"] <= 800

        # Per-session cap (<= 2) remains enforced across 4 sessions x 4 chunks
        tf = tmp_path / "multi_sess.jsonl"
        lines = []
        for s in range(4):
            for k in range(4):
                lines.append(json.dumps({
                    "type": "assistant", "sessionId": f"s_{s}", "cwd": str(tmp_path),
                    "timestamp": f"2026-11-12T10:0{s}:0{k}Z",
                    "message": {"role": "assistant", "content": f"Session {s} chunk {k}: multi_sess_token rule."},
                }))
        tf.write_text("\n".join(lines) + "\n", encoding="utf-8")
        st.ingest_file(tf, "claude")
        res2 = st.search("multi_sess_token", tmp_path.name, budget_tokens=800)
        chunk_res = [r for r in res2["results"] if r["kind"] is None]
        assert len(chunk_res) == 8  # 4 sessions * 2 per session (> 5)
        counts = Counter(r["session_id"] for r in chunk_res)
        assert all(v <= 2 for v in counts.values())
    finally:
        st.close()


def test_git_worktree_project_and_config_aliases(tmp_path, monkeypatch):
    import subprocess
    from mesa_lore.store import get_project

    main_repo = tmp_path / "mesa-legal-data"
    main_repo.mkdir()
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "T"],
        ["git", "commit", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(cmd, cwd=main_repo, check=True, capture_output=True)
    wt = tmp_path / "wt_law"
    subprocess.run(["git", "worktree", "add", str(wt), "-b", "wt-branch"], cwd=main_repo, check=True, capture_output=True)
    sub = wt / "nested" / "dir"
    sub.mkdir(parents=True)

    assert get_project(str(wt)) == "mesa-legal-data"
    assert get_project(str(sub)) == "mesa-legal-data"

    home = tmp_path / "home"
    cfg = home / ".config" / "mesa-lore" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('excluded_projects = ["priv"]\n\n[aliases]\n"data-law" = "mesa-legal-data"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    import sys
    from unittest.mock import patch
    from mesa_lore.store import _load_config
    with patch.dict(sys.modules, {"tomllib": None}):
        assert _load_config(home) == ({"priv"}, {"data-law": "mesa-legal-data"})

    st = Store(tmp_path / "alias.db")
    try:
        old_id = st.save_note("data-law", "Legacy parquet compression zstd decision", "decision")
        new_id = st.save_note("mesa-legal-data", "Current schema version check note", "fact")
        st.save_note("other-proj", "Other project parquet compression note", "decision")

        res = st.search("parquet compression zstd", "mesa-legal-data", budget_tokens=400)
        ids = {r["id"] for r in res["results"]}
        assert old_id in ids and not any(r["project"] == "other-proj" for r in res["results"])
        assert {r["id"] for r in st.search("schema version check", "data-law", budget_tokens=400)["results"]} >= {new_id}
        assert st.get(old_id, project="mesa-legal-data") is not None
        idx = st.index("mesa-legal-data")
        assert "parquet compression" in idx or "schema version" in idx
        exp = st.export("mesa-legal-data")
        assert "Legacy parquet" in exp and "Current schema" in exp and "Other project" not in exp
        assert {r["id"] for r in st.list_entries("mesa-legal-data")} >= {old_id, new_id}
    finally:
        st.close()


def test_recency_tie_break_and_full_date_in_mcp_search(tmp_path, monkeypatch):
    from mesa_lore.mcp_server import memory_search

    tf = tmp_path / "dates.jsonl"
    tf.write_text("\n".join([
        json.dumps({"type": "assistant", "sessionId": "s_old", "cwd": str(tmp_path), "timestamp": "2026-10-01T10:00:00Z",
                    "message": {"role": "assistant", "content": "Set cache_eviction_policy cache_eviction_policy to LRU with 128MB limit."}}),
        json.dumps({"type": "assistant", "sessionId": "s_new", "cwd": str(tmp_path), "timestamp": "2026-11-12T10:00:00Z",
                    "message": {"role": "assistant", "content": "Set cache_eviction_policy to FIFO with 256MB limit for latency."}}),
        json.dumps({"type": "assistant", "sessionId": "s_part", "cwd": str(tmp_path), "timestamp": "2026-11-13T10:00:00Z",
                    "message": {"role": "assistant", "content": "Reviewed latency logs without mentioning policy."}}),
    ]) + "\n", encoding="utf-8")

    db = tmp_path / "recency.db"
    st = Store(db)
    try:
        st.ingest_file(tf, "claude")
        res = st.search("cache_eviction_policy limit", tmp_path.name, budget_tokens=800)
        assert res["status"] == "ok"
        assert res["results"][0]["session_id"] == "s_new"
        # Lower-relevance partial match ("latency" only) still cannot beat strong matches via recency
        assert not any(r["session_id"] == "s_part" for r in res["results"])
    finally:
        st.close()

    monkeypatch.setenv("MESA_LORE_DB", str(db))
    out = memory_search("cache_eviction_policy limit", project=tmp_path.name)
    assert "2026-11-12" in out
