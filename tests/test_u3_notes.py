import json
import tempfile
from pathlib import Path
from mesa_lore.store import Store


def test_save_note_and_verify(tmp_path):
    # Ingest transcript
    transcript = tmp_path / "claude.jsonl"
    lines = [
        {"type": "user", "sessionId": "sess1", "cwd": "/my/proj", "timestamp": "2026-01-01T10:00:00Z",
         "message": {"role": "user", "content": "Tamam, PostgreSQL'e karar verdik."}},
        {"type": "user", "sessionId": "sess1", "cwd": "/my/proj", "timestamp": "2026-01-01T10:05:00Z",
         "message": {"role": "user", "content": "Redis cache   denedik ama ÇALIŞMADI, vazgeçtik."}}
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

    db = tmp_path / "lore.db"
    st = Store(db)
    st.ingest_file(transcript, agent="claude")

    # 1. Matching quote
    n1_id = st.save_note("proj", "PostgreSQL seçildi.", "decision", session_id="sess1", quote="PostgreSQL'e karar verdik")
    n1 = st.get(n1_id)
    assert n1["verified"] is True
    assert n1["state"] == "active"
    assert n1["source_type"] == "note"

    # 2. Matching quote with Turkish lower & whitespace collapsing
    n2_id = st.save_note("proj", "Redis bırakıldı.", "failed_attempt", session_id="sess1", quote="redis   cache denedik   ama çalışmadı")
    n2 = st.get(n2_id)
    assert n2["verified"] is True

    # 3. Non-matching quote
    n3_id = st.save_note("proj", "Kafka istendi.", "fact", session_id="sess1", quote="Kafka kullanalım")
    n3 = st.get(n3_id)
    assert n3["verified"] is False

    # 4. Missing quote / session_id
    n4_id = st.save_note("global", "Kısa cevap sever.", "fact")
    n4 = st.get(n4_id)
    assert n4["verified"] is False
    assert n4["project"] == "global"

    # 5. Supersession
    n5_id = st.save_note("proj", "SQLite'a geçildi.", "decision", replaces=n1_id)
    n1_updated = st.get(n1_id)
    assert n1_updated["state"] == "superseded"
    n5 = st.get(n5_id)
    assert n5["state"] == "active"

    # 6. Secret masking in note
    n6_id = st.save_note("proj", "Token: ghp_FAKEtoken00000000000000000000000000AB", "fact")
    n6 = st.get(n6_id)
    assert "ghp_FAKEtoken" not in n6["text"]
    assert "[REDACTED]" in n6["text"]

    # 7. Non-existent ID
    assert st.get("nonexistent") is None

    st.close()
