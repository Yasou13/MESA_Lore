import json
import sqlite3

from mesa_lore.store import SCHEMA_VERSION, Store


def _claude_line(text, sid="s1"):
    return json.dumps({"type": "user", "sessionId": sid, "cwd": "/repo/app", "timestamp": "2026-11-20T10:00:00Z",
                       "message": {"role": "user", "content": text}})


def test_ingest_creates_no_candidate_notes(tmp_path):
    p = tmp_path / "s1.jsonl"
    p.write_text(_claude_line("Kuyruk için NATS kullanmaya karar verdik.\n# NOTE: clock must be monotonic") + "\n", encoding="utf-8")
    st = Store(tmp_path / "lore.db")
    try:
        assert st.ingest_file(p, "claude") == 1
        assert st.con.execute("SELECT count(*) FROM entries WHERE source_type = 'note'").fetchone()[0] == 0
    finally:
        st.close()


def test_migration_removes_candidate_rows_and_keeps_the_rest(tmp_path):
    db = tmp_path / "lore.db"
    st = Store(db)
    keep = st.save_note("app", "NATS is the queue", "decision")
    st.con.execute("DELETE FROM schema_version WHERE version > 8")
    st.con.execute("INSERT INTO entries (id, project, text, source_type, state, kind) VALUES ('n_cand', 'app', 'candidate zebra', 'note', 'candidate', 'fact')")
    st.con.execute("INSERT INTO entries_fts (id, text_norm) VALUES ('n_cand', 'candidate zebra')")
    st.con.commit()
    st.close()
    st = Store(db)
    try:
        assert st._current_schema_version() == SCHEMA_VERSION
        assert st.get("n_cand") is None and st.get(keep)["text"] == "NATS is the queue"
        assert st.con.execute("SELECT count(*) FROM entries_fts WHERE id = 'n_cand'").fetchone()[0] == 0
    finally:
        st.close()


def test_candidate_state_is_gone_from_the_schema_docs_of_search(tmp_path):
    db = tmp_path / "lore.db"
    st = Store(db)
    try:
        st.save_note("app", "zebra routing uses NATS", "fact")
        assert all(r["state"] == "active" for r in st.search("zebra routing", "app")["results"])
    finally:
        st.close()
    assert sqlite3.connect(db).execute("SELECT count(*) FROM entries WHERE state = 'candidate'").fetchone()[0] == 0
