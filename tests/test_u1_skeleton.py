import sqlite3
import tempfile
from pathlib import Path
from mesa_lore.store import Store
from mesa_lore.text import tr_lower, mask_secrets


def test_store_memory():
    st = Store(":memory:")
    assert st.con is not None
    st.close()


def test_store_file_wal_and_schema():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        st = Store(db_path)
        cur = st.con.cursor()
        mode = cur.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        timeout = cur.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout >= 1000

        # Check schema
        tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "entries" in tables
        st.close()


def test_tr_lower():
    assert tr_lower("İSTANBUL") == "istanbul"
    assert tr_lower("IŞIK") == "ışık"
    assert tr_lower("VERİTABANINI") == "veritabanını"
    assert tr_lower("DAKİKADA KAÇ İSTEK") == "dakikada kaç istek"


def test_mask_secrets(tmp_path):
    secrets = [
        ("sk-proj-FAKEPROJKEY1234567890abcdef", "key is sk-proj-FAKEPROJKEY1234567890abcdef"),
        ("sk-ant-api03-FAKEKEY1234567890abcdefXYZ", "API key: sk-ant-api03-FAKEKEY1234567890abcdefXYZ"),
        ("ghp_FAKEtoken00000000000000000000000000AB", "GitHub: ghp_FAKEtoken00000000000000000000000000AB"),
        ("github_pat_11AA22BB33CC44DD55EE66FF", "PAT: github_pat_11AA22BB33CC44DD55EE66FF"),
        ("AKIAIOSFODNN7EXAMPLE", "AWS: AKIAIOSFODNN7EXAMPLE"),
        ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.FAKESIG", "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.FAKESIG"),
        ("SuperSecretPass99!", "db_url password=SuperSecretPass99! host=localhost"),
        ("my_secret_api_key_value_123", "config api_key: my_secret_api_key_value_123"),
        ("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC", "-----BEGIN RSA PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC\n-----END RSA PRIVATE KEY-----"),
    ]
    db_path = tmp_path / "secrets.db"
    st = Store(db_path)
    for raw_sec, snippet in secrets:
        masked = mask_secrets(snippet)
        assert raw_sec not in masked, f"Secret {raw_sec!r} was not masked in {masked!r}"
        assert "[REDACTED]" in masked
        st.save_note("proj", snippet, "fact", session_id="s1", quote=snippet)

    # Check raw DB bytes including WAL while connection is open and after close
    raw_open = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    for raw_sec, _ in secrets:
        assert raw_sec.encode() not in raw_open, f"Leaked {raw_sec!r} in open DB/WAL bytes"
    st.close()
    raw_closed = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    for raw_sec, _ in secrets:
        assert raw_sec.encode() not in raw_closed, f"Leaked {raw_sec!r} in closed DB bytes"


def test_concurrent_two_process_writes(tmp_path):
    import subprocess
    import sys
    db_path = tmp_path / "concurrent.db"
    transcript = tmp_path / "shared.jsonl"
    transcript.write_text(
        '{"type": "user", "sessionId": "sc1", "cwd": "/repo/proj", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "Concurrent ingest line"}}\n',
        encoding="utf-8"
    )
    worker_code = """
import sys
from mesa_lore.store import Store
db_path, t_path, proc_id = sys.argv[1], sys.argv[2], sys.argv[3]
for i in range(30):
    st = Store(db_path)
    st.ingest_file(t_path, agent="claude")
    st.save_note("proj", f"Concurrent note from {proc_id} #{i}", "fact")
    st.close()
"""
    p1 = subprocess.Popen([sys.executable, "-c", worker_code, str(db_path), str(transcript), "p1"], stderr=subprocess.PIPE, text=True)
    p2 = subprocess.Popen([sys.executable, "-c", worker_code, str(db_path), str(transcript), "p2"], stderr=subprocess.PIPE, text=True)
    _, err1 = p1.communicate(timeout=15)
    _, err2 = p2.communicate(timeout=15)
    assert p1.returncode == 0, f"Process 1 failed: {err1}"
    assert p2.returncode == 0, f"Process 2 failed: {err2}"

    st = Store(db_path)
    notes_cnt = st.con.execute("SELECT COUNT(*) FROM entries WHERE source_type = 'note'").fetchone()[0]
    chunks_cnt = st.con.execute("SELECT COUNT(*) FROM entries WHERE source_type = 'user'").fetchone()[0]
    st.close()
    assert notes_cnt == 60
    assert chunks_cnt == 1
