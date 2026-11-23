import json
import math
from mesa_lore.store import Store


def test_mechanical_handoff_generated_and_idempotent(tmp_path):
    tf = tmp_path / "sess.jsonl"
    lines = [
        {
            "type": "user", "sessionId": "s_h1", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:00:00Z",
            "message": {"role": "user", "content": "Implement JWT refresh rotation in auth module"},
        },
        {
            "type": "assistant", "sessionId": "s_h1", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:00:05Z",
            "message": {
                "role": "assistant", "model": "claude-opus-5-5",
                "content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "/repo/src/auth.py"}}],
            },
        },
        {
            "type": "user", "sessionId": "s_h1", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:00:06Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        },
        {
            "type": "assistant", "sessionId": "s_h1", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:00:07Z",
            "message": {
                "role": "assistant", "model": "claude-opus-5-5",
                "content": [{"type": "tool_use", "id": "t2", "name": "apply_patch", "input": "*** Update File: src/tokens.py\n@@"}],
            },
        },
        {
            "type": "user", "sessionId": "s_h1", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:00:08Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]},
        },
        {
            "type": "assistant", "sessionId": "s_h1", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:00:10Z",
            "message": {"role": "assistant", "model": "claude-opus-5-5", "content": "Added TOKEN_ROTATE_TTL=900 and tests pass."},
        },
    ]
    tf.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        proj = tmp_path.name
        st.save_note(proj, "Add rate limiter to /refresh", "todo")
        assert st.ingest_file(tf, "claude") >= 2

        handoffs = [e for e in st.list_entries(proj) if e["source_type"] == "note" and e["kind"] == "handoff" and e["session_id"] == "s_h1"]
        assert len(handoffs) == 1
        h = handoffs[0]
        assert h["state"] == "active"
        assert "JWT refresh rotation" in h["text"]
        assert "TOKEN_ROTATE_TTL=900" in h["text"]
        assert "auth.py" in h["text"]
        assert "tokens.py" in h["text"]
        assert "rate limiter" in h["text"]
        assert math.ceil(len(h["text"]) / 4) <= 400

        # Re-ingest is idempotent (still 1 active handoff)
        assert st.ingest_file(tf, "claude") == 0
        handoffs2 = [e for e in st.list_entries(proj) if e["kind"] == "handoff" and e["session_id"] == "s_h1"]
        assert len(handoffs2) == 1

        # Search by kind="handoff" works
        res = st.search("TOKEN_ROTATE_TTL", proj, budget_tokens=400, kind="handoff")
        assert res["status"] == "ok"

        # Appending a new turn updates the single active handoff cleanly
        with open(tf, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "assistant", "sessionId": "s_h1", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:05:00Z",
                "message": {"role": "assistant", "model": "claude-opus-5-5", "content": "Finalized FINAL_RELEASE_TAG_42"},
            }) + "\n")
        assert st.ingest_file(tf, "claude") == 1
        handoffs3 = [e for e in st.list_entries(proj) if e["kind"] == "handoff" and e["session_id"] == "s_h1"]
        assert len(handoffs3) == 1
        assert "FINAL_RELEASE_TAG_42" in handoffs3[0]["text"]
    finally:
        st.close()


def test_handoff_token_cap_and_secret_masking(tmp_path):
    tf = tmp_path / "huge.jsonl"
    big = "wordtoken " * 600
    tf.write_text("\n".join([
        json.dumps({
            "type": "user", "sessionId": "s_big", "cwd": str(tmp_path), "timestamp": "2026-11-10T11:00:00Z",
            "message": {"role": "user", "content": f"Check key sk-ant-secretvalue1234567890 {big}"},
        }),
        json.dumps({
            "type": "assistant", "sessionId": "s_big", "cwd": str(tmp_path), "timestamp": "2026-11-10T11:01:00Z",
            "message": {"role": "assistant", "content": f"Processed {big}"},
        }),
    ]) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(tf, "claude")
        handoffs = [e for e in st.list_entries(tmp_path.name) if e["kind"] == "handoff"]
        assert len(handoffs) == 1
        assert math.ceil(len(handoffs[0]["text"]) / 4) <= 400
        assert "sk-ant-secretvalue" not in handoffs[0]["text"]
    finally:
        st.close()
