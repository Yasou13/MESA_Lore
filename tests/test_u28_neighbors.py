import json
from mesa_lore.mcp_server import TOOL_SPECS, memory_get
from mesa_lore.store import Store


def test_store_get_neighbors_same_session_and_isolated(tmp_path):
    tf1 = tmp_path / "s1.jsonl"
    tf1.write_text("\n".join([
        json.dumps({"type": "user", "sessionId": "sess1", "cwd": "/tmp/depo-sync", "timestamp": "2026-11-10T10:00:00Z", "message": {"role": "user", "content": "Check config SYNC_PORT=8443"}}),
        json.dumps({"type": "assistant", "sessionId": "sess1", "cwd": "/tmp/depo-sync", "timestamp": "2026-11-10T10:00:01Z", "message": {"role": "assistant", "content": "Rotating key encryption service"}}),
        json.dumps({"type": "assistant", "sessionId": "sess1", "cwd": "/tmp/depo-sync", "timestamp": "2026-11-10T10:00:02Z", "message": {"role": "assistant", "content": "Switched cipher to CHACHA20_POLY1305"}}),
    ]) + "\n", encoding="utf-8")

    tf2 = tmp_path / "s2.jsonl"
    tf2.write_text("\n".join([
        json.dumps({"type": "user", "sessionId": "sess2", "cwd": "/tmp/depo-sync", "timestamp": "2026-11-10T11:00:00Z", "message": {"role": "user", "content": "OTHER_SESSION_TEXT"}}),
        json.dumps({"type": "user", "sessionId": "sess3", "cwd": "/tmp/other-proj", "timestamp": "2026-11-10T12:00:00Z", "message": {"role": "user", "content": "OTHER_PROJ_TEXT"}}),
    ]) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(tf1, "claude")
        st.ingest_file(tf2, "claude")
        nid = st.save_note("depo-sync", "Important session note", "fact", session_id="sess1")

        mid = next(e["id"] for e in st.list_entries("depo-sync") if e["source_type"] != "note" and "Rotating key" in e["text"])
        g = st.get(mid, project="depo-sync", before=1, after=1)
        assert g is not None
        assert len(g["before"]) == 1 and "SYNC_PORT=8443" in g["before"][0]["text"]
        assert len(g["after"]) == 1 and "CHACHA20_POLY1305" in g["after"][0]["text"]

        # Clamped at session boundary, never bleeds into other sessions/projects or notes
        g_wide = st.get(mid, project="depo-sync", before=5, after=5)
        all_neighbor_texts = " ".join(x["text"] for x in g_wide["before"] + g_wide["after"])
        assert "OTHER_SESSION_TEXT" not in all_neighbor_texts
        assert "OTHER_PROJ_TEXT" not in all_neighbor_texts
        assert "Important session note" not in all_neighbor_texts

        # Cross-project isolation
        assert st.get(mid, project="other-proj", before=1, after=1) is None

        # Note has no neighbors
        ng = st.get(nid, project="depo-sync", before=1, after=1)
        assert ng["before"] == [] and ng["after"] == []

        # Default before=0, after=0 keeps baseline schema
        plain = st.get(mid, project="depo-sync")
        assert "before" not in plain and "after" not in plain
    finally:
        st.close()


def test_mcp_memory_get_with_neighbors(tmp_path, monkeypatch):
    db = tmp_path / "lore.db"
    monkeypatch.setenv("MESA_LORE_DB", str(db))
    tf = tmp_path / "s.jsonl"
    tf.write_text("\n".join([
        json.dumps({"type": "user", "sessionId": "s1", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:00:00Z", "message": {"role": "user", "content": "BEFORE_NEIGHBOR_99"}}),
        json.dumps({"type": "assistant", "sessionId": "s1", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:00:01Z", "message": {"role": "assistant", "content": "TARGET_CHUNK_42"}}),
        json.dumps({"type": "assistant", "sessionId": "s1", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:00:02Z", "message": {"role": "assistant", "content": "AFTER_NEIGHBOR_77"}}),
    ]) + "\n", encoding="utf-8")

    st = Store(db)
    st.ingest_file(tf, "claude")
    mid = next(e["id"] for e in st.list_entries(tmp_path.name) if e["source_type"] != "note" and "TARGET_CHUNK_42" in e["text"])
    st.close()

    spec = next(s for s in TOOL_SPECS if s["name"] == "memory_get")
    assert "before" in spec["inputSchema"]["properties"]
    assert "after" in spec["inputSchema"]["properties"]

    out = memory_get(mid, project=tmp_path.name, before=1, after=1)
    assert out.startswith("<untrusted_memory>")
    assert "BEFORE_NEIGHBOR_99" in out
    assert "TARGET_CHUNK_42" in out
    assert "AFTER_NEIGHBOR_77" in out
