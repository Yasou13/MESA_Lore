import json
import subprocess
import sys
from pathlib import Path
from mesa_lore.store import SCHEMA_VERSION, Store


def test_corrupt_file_records_warning_and_doctor_reports_it(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{broken json\n{"foo": "bar"}\n["array"]\n', encoding="utf-8")
    db = tmp_path / "lore.db"
    st = Store(db)
    try:
        assert st.ingest_file(bad, "claude") == 0
        rows = st.con.execute("SELECT path, agent, reason FROM ingest_warnings").fetchall()
        assert len(rows) == 1
        assert "bad.jsonl" in rows[0]["path"]
        assert rows[0]["agent"] == "claude"
    finally:
        st.close()

    res = subprocess.run(
        [sys.executable, "-m", "mesa_lore.cli", "doctor", "--home", str(tmp_path)],
        env={"PATH": "", "MESA_LORE_DB": str(db), "HOME": str(tmp_path), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        capture_output=True, text=True,
    )
    assert res.returncode == 0
    low = res.stdout.lower()
    assert "warn" in low
    assert "bad.jsonl" in res.stdout


def test_unrecognized_valid_json_records_warning(tmp_path):
    f = tmp_path / "unknown.jsonl"
    f.write_text('{"completely": "different"}\n{"another": 42}\n', encoding="utf-8")
    st = Store(tmp_path / "lore.db")
    try:
        assert st.ingest_file(f, "claude") == 0
        rows = st.con.execute("SELECT reason FROM ingest_warnings").fetchall()
        assert len(rows) == 1
        assert "unrecognized" in rows[0]["reason"].lower() or "0 chunk" in rows[0]["reason"].lower()
    finally:
        st.close()


def test_codex_subagent_skip_produces_no_warning(tmp_path):
    f = tmp_path / "subagent.jsonl"
    f.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "sub1", "cwd": str(tmp_path), "source": {"subagent": "spawn"}},
    }) + "\n", encoding="utf-8")
    st = Store(tmp_path / "lore.db")
    try:
        assert st.ingest_file(f, "codex") == 0
        assert st.con.execute("SELECT count(*) FROM ingest_warnings").fetchone()[0] == 0
    finally:
        st.close()


def test_v1_migrates_to_v2_with_warnings_table(tmp_path):
    import sqlite3
    db = tmp_path / "v1.db"
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
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES (1, '2026-11-01T00:00:00Z');
    """)
    con.commit()
    con.close()

    st = Store(db)
    try:
        assert st.con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        assert st.con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ingest_warnings'").fetchone()
    finally:
        st.close()
