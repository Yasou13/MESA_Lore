import shutil
import sqlite3
from pathlib import Path
from mesa_lore.store import SCHEMA_VERSION, Store

R3_FIXTURE = Path(__file__).resolve().parents[1] / "bench" / "fixtures" / "r3_schema.db"


def test_fresh_db_records_schema_version(tmp_path):
    db = tmp_path / "fresh.db"
    st = Store(db)
    try:
        rows = st.con.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
        assert rows
        assert rows[-1][0] == SCHEMA_VERSION >= 1
    finally:
        st.close()


def test_r3_synthetic_db_migrates_without_data_loss(tmp_path):
    db = tmp_path / "legacy_r3.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE entries (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, text TEXT NOT NULL, source_type TEXT NOT NULL,
            session_id TEXT, ts TEXT, state TEXT NOT NULL DEFAULT 'active', verified INTEGER,
            stale_hint INTEGER DEFAULT 0, kind TEXT, quote TEXT, replaces TEXT, files TEXT,
            repo TEXT, file_hashes TEXT, dedupe_key TEXT UNIQUE, agent TEXT, model TEXT
        );
        CREATE VIRTUAL TABLE entries_fts USING fts5(id UNINDEXED, text_norm, tokenize='trigram');
        CREATE TABLE usage (
            session_id TEXT NOT NULL, agent TEXT NOT NULL, project TEXT NOT NULL, ts TEXT,
            uncached_input INTEGER NOT NULL, cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL,
            output INTEGER NOT NULL, memory_used INTEGER NOT NULL DEFAULT 0, dedupe_key TEXT UNIQUE
        );
        INSERT INTO entries (id, project, text, source_type, session_id, ts, state, dedupe_key)
        VALUES ('n_old', 'kargo-api', 'PostgreSQL migration note', 'note', 's1', '2026-11-01T10:00:00Z', 'active', 'k1');
        INSERT INTO entries_fts (id, text_norm) VALUES ('n_old', 'postgresql migration note');
    """)
    con.commit()
    con.close()

    st = Store(db)
    try:
        ver = st.con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert ver == SCHEMA_VERSION >= 1
        assert st.get("n_old")["text"] == "PostgreSQL migration note"
        res = st.search("PostgreSQL", "kargo-api", budget_tokens=300)
        assert res["status"] == "ok"
        nid = st.save_note("kargo-api", "Redis cache decision", "decision")
        assert st.get(nid)["text"] == "Redis cache decision"
    finally:
        st.close()

    # Re-opening must stay idempotent
    st2 = Store(db)
    try:
        rows = st2.con.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
        assert rows[-1][0] == SCHEMA_VERSION
        assert st2.get("n_old") is not None
    finally:
        st2.close()


def test_r3_fixture_migrates_non_destructively(tmp_path):
    if not R3_FIXTURE.exists():
        return
    db = tmp_path / "r3_copy.db"
    shutil.copy2(R3_FIXTURE, db)
    db.chmod(0o644)
    before_count = sqlite3.connect(db).execute("SELECT count(*) FROM entries").fetchone()[0]

    st = Store(db)
    try:
        after_count = st.con.execute("SELECT count(*) FROM entries").fetchone()[0]
        assert after_count == before_count
        ver = st.con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert ver == SCHEMA_VERSION >= 1
        assert st.search("PostgreSQL", "kargo-api", budget_tokens=300)["status"] == "ok"
        nid = st.save_note("kargo-api", "post-migration note", "fact")
        assert st.get(nid)["text"] == "post-migration note"
    finally:
        st.close()
