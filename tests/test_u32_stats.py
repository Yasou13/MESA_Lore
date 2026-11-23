import json
import os
import sqlite3
import sys
from io import StringIO
from unittest.mock import patch
from mesa_lore.cli import main
from mesa_lore.store import SCHEMA_VERSION, Store


def test_stats_u32_agent_first10_notes_and_handoff(tmp_path):
    # Session 1 (claude): opened with SessionStart handoff context + 12 turns (turns 10-11 huge cost excluded from First10)
    s1 = tmp_path / "cl.jsonl"
    cl_lines = [
        {"type": "user", "sessionId": "cl1", "cwd": "/repo/app", "timestamp": "2026-11-10T10:00:00Z",
         "message": {"role": "user", "content": "<untrusted_memory>\nScopes: app (1)\nHandoffs:\n- 2026-11-09: set up app\n</untrusted_memory>\nHi"}},
    ]
    for i in range(12):
        cost_in = 100 if i < 10 else 9999
        cl_lines.append({
            "type": "assistant", "sessionId": "cl1", "cwd": "/repo/app", "timestamp": f"2026-11-10T10:00:{i+1:02d}Z",
            "message": {"id": f"m{i}", "role": "assistant", "content": f"step {i}",
                        "usage": {"input_tokens": cost_in, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 10}},
        })
    s1.write_text("\n".join(json.dumps(x) for x in cl_lines) + "\n", encoding="utf-8")

    # Session 2 (codex): mid-session memory_search (memory_used=1, handoff_used=0)
    s2 = tmp_path / "cx.jsonl"
    s2.write_text("\n".join([
        json.dumps({"type": "session_meta", "timestamp": "2026-11-10T11:00:00Z", "payload": {"id": "cx1", "cwd": "/repo/app"}}),
        json.dumps({"type": "response_item", "timestamp": "2026-11-10T11:00:01Z",
                    "payload": {"type": "function_call", "name": "mcp__mesa-lore__memory_search", "arguments": "{}", "call_id": "c1"}}),
        json.dumps({"type": "event_msg", "timestamp": "2026-11-10T11:00:02Z",
                    "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 500, "cached_input_tokens": 0, "output_tokens": 20}}}}),
    ]) + "\n", encoding="utf-8")

    # Session 3 (claude): no memory, no handoff
    s3 = tmp_path / "cl2.jsonl"
    s3.write_text("\n".join([
        json.dumps({"type": "user", "sessionId": "cl2", "cwd": "/repo/app", "timestamp": "2026-11-10T12:00:00Z",
                    "message": {"role": "user", "content": "plain question"}}),
        json.dumps({"type": "assistant", "sessionId": "cl2", "cwd": "/repo/app", "timestamp": "2026-11-10T12:00:01Z",
                    "message": {"id": "m_plain", "role": "assistant", "content": "plain answer",
                                "usage": {"input_tokens": 300, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 10}}}),
    ]) + "\n", encoding="utf-8")

    db = tmp_path / "lore.db"
    st = Store(db)
    try:
        st.ingest_file(s1, "claude")
        st.ingest_file(s2, "codex")
        st.ingest_file(s3, "claude")
        for i in range(3):
            st.save_note("app", f"Saved note #{i}", "fact", agent="claude")

        all_d = st.stats(project="app")
        comp = all_d["memory_comparison"]
        assert comp["with_memory"]["sessions"] == 2
        assert comp["without_memory"]["sessions"] == 1
        assert comp["with_handoff"]["sessions"] == 1
        assert comp["with_handoff"]["avg_first_10_cost"] == 1000
        assert all_d["notes_count"] == 3
        assert all_d["notes_per_session"] == 1.0  # 3 notes / 3 sessions

        codex_d = st.stats(project="app", agent="codex")
        assert [a["agent"] for a in codex_d["agents"]] == ["codex"]
        assert codex_d["memory_comparison"]["with_handoff"]["sessions"] == 0
    finally:
        st.close()

    buf = StringIO()
    with patch.dict(os.environ, {"MESA_LORE_DB": str(db)}):
        with patch.object(sys, "argv", ["mesa-lore", "stats", "--project", "app", "--agent", "claude"]):
            with patch("sys.stdout", buf):
                main()
    out = buf.getvalue()
    assert "claude" in out and "codex" not in out
    assert "With handoff" in out
    assert "First10" in out
    assert "Notes" in out


def test_v4_migrates_to_v5_adding_handoff_used(tmp_path):
    db = tmp_path / "v4.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE entries (id TEXT PRIMARY KEY, project TEXT NOT NULL, text TEXT NOT NULL, source_type TEXT NOT NULL,
            session_id TEXT, ts TEXT, state TEXT NOT NULL DEFAULT 'active', verified INTEGER, stale_hint INTEGER DEFAULT 0,
            kind TEXT, quote TEXT, replaces TEXT, files TEXT, repo TEXT, file_hashes TEXT, dedupe_key TEXT UNIQUE, agent TEXT, model TEXT);
        CREATE VIRTUAL TABLE entries_fts USING fts5(id UNINDEXED, text_norm, tokenize='trigram');
        CREATE TABLE usage (session_id TEXT NOT NULL, agent TEXT NOT NULL, project TEXT NOT NULL, ts TEXT,
            uncached_input INTEGER NOT NULL, cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL,
            output INTEGER NOT NULL, memory_used INTEGER NOT NULL DEFAULT 0, dedupe_key TEXT UNIQUE);
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES (4, '2026-11-10T00:00:00Z');
    """)
    con.commit()
    con.close()

    st = Store(db)
    try:
        cols = {r[1] for r in st.con.execute("PRAGMA table_info(usage)").fetchall()}
        assert "handoff_used" in cols
        assert st.con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        st.close()
