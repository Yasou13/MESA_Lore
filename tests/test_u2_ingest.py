import json
import tempfile
from pathlib import Path
from mesa_lore.store import Store


def test_ingest_claude(tmp_path):
    transcript = tmp_path / "claude.jsonl"
    lines = [
        {"type": "user", "sessionId": "s1", "cwd": "/path/to/my-proj", "timestamp": "2026-01-01T10:00:00Z",
         "message": {"role": "user", "content": "Hello secret sk-ant-api03-FAKEKEY1234567890abcdefXYZ"}},
        {"type": "assistant", "sessionId": "s1", "cwd": "/path/to/my-proj", "timestamp": "2026-01-01T10:00:05Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "Internal thoughts"},
             {"type": "text", "text": "Assistant response"},
             {"type": "tool_use", "id": "call1", "name": "bash", "input": {"cmd": "ls"}}
         ]}},
        {"type": "user", "sessionId": "s1", "cwd": "/path/to/my-proj", "timestamp": "2026-01-01T10:00:10Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "call1", "content": "file1.txt\nfile2.txt"}
         ]}},
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

    db = tmp_path / "lore.db"
    st = Store(db)
    new_chunks = st.ingest_file(transcript, agent="claude")
    # 3 chunks: user message, assistant text, merged tool call+output
    assert new_chunks == 3

    # Re-ingest gives 0
    assert st.ingest_file(transcript, agent="claude") == 0

    rows = st.con.execute("SELECT * FROM entries ORDER BY ts").fetchall()
    assert len(rows) == 3
    # Check secrets masked
    assert "sk-ant-api03" not in rows[0]["text"]
    assert "[REDACTED]" in rows[0]["text"]
    assert rows[0]["project"] == "my-proj"
    assert rows[0]["source_type"] == "user"

    # Check assistant text (thinking skipped)
    assert rows[1]["source_type"] == "assistant"
    assert rows[1]["text"] == "Assistant response"

    # Check tool call + output merged
    assert rows[2]["source_type"] == "tool"
    assert "bash" in rows[2]["text"]
    assert "file1.txt" in rows[2]["text"]

    st.close()


def test_ingest_codex(tmp_path):
    transcript = tmp_path / "codex.jsonl"
    lines = [
        {"type": "session_meta", "timestamp": "2026-01-01T10:00:00Z",
         "payload": {"id": "cx1", "cwd": "/repo/sub/app-code"}},
        {"type": "response_item", "timestamp": "2026-01-01T10:00:00Z",
         "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "Developer system instructions"}]}},
        {"type": "response_item", "timestamp": "2026-01-01T10:00:01Z",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Deploy to prod"}]}},
        {"type": "response_item", "timestamp": "2026-01-01T10:00:02Z",
         "payload": {"type": "function_call", "name": "run", "arguments": "npm run build", "call_id": "fn1"}},
        {"type": "response_item", "timestamp": "2026-01-01T10:00:03Z",
         "payload": {"type": "function_call_output", "call_id": "fn1", "output": "Build succeeded"}},
        {"type": "response_item", "timestamp": "2026-01-01T10:00:04Z",
         "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch", "call_id": "ct1"}},
        {"type": "response_item", "timestamp": "2026-01-01T10:00:05Z",
         "payload": {"type": "custom_tool_call_output", "call_id": "ct1", "output": "Success. Updated files."}},
        {"type": "event_msg", "payload": {"type": "token_count"}},
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    db = tmp_path / "lore.db"
    st = Store(db)
    new_chunks = st.ingest_file(transcript, agent="codex")
    # 3 chunks: user message, merged function call+output, merged custom_tool_call+output (developer & token_count skipped)
    assert new_chunks == 3
    assert st.ingest_file(transcript, agent="codex") == 0

    rows = st.con.execute("SELECT * FROM entries WHERE source_type='tool' ORDER BY ts").fetchall()
    assert len(rows) == 2
    assert "run npm run build" in rows[0]["text"]
    assert "Build succeeded" in rows[0]["text"]
    assert "apply_patch *** Begin Patch" in rows[1]["text"]
    assert "Success. Updated files." in rows[1]["text"]

    # Ensure developer role was skipped
    all_texts = [r["text"] for r in st.con.execute("SELECT text FROM entries").fetchall()]
    assert not any("Developer system instructions" in t for t in all_texts)

    # Append new lines to transcript and re-ingest: only new lines added
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "response_item", "timestamp": "2026-01-01T10:00:06Z",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Deployment complete."}]}
        }) + "\n")
    assert st.ingest_file(transcript, agent="codex") == 1
    assert st.ingest_file(transcript, agent="codex") == 0

    st.close()


def test_ingest_malformed_lines(tmp_path):
    transcript = tmp_path / "malformed.jsonl"
    content = (
        '{"type": "user", "sessionId": "s1", "cwd": "/proj", "timestamp": "2026-01-01", "message": {"content": "ok1"}}\n'
        'NOT VALID JSON {{{{}}]\n'
        '\n'
        '12345\n'
        '{"type": "user", "sessionId": "s1", "cwd": "/proj", "timestamp": "2026-01-02", "message": {"content": "ok2"}}\n'
    )
    transcript.write_text(content, encoding="utf-8")

    db = tmp_path / "lore.db"
    st = Store(db)
    new_chunks = st.ingest_file(transcript, agent="claude")
    assert new_chunks == 2
    st.close()
