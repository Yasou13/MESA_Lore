"""R3 review regression tests: transcript parsing (no dev or holdout questions)."""
import json
import math
import os
import sys
from io import StringIO
from unittest.mock import patch

from mesa_lore import cli, mcp_server
from mesa_lore.store import Store
from mesa_lore.text import mask_secrets


def test_injected_context_is_not_stored_as_user_text(tmp_path):
    cl = tmp_path / "c.jsonl"
    cl.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "isMeta": True, "sessionId": "s1", "cwd": "/r/p", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "[Image: source: /tmp/a.png]"}]}},
        {"type": "user", "sessionId": "s1", "cwd": "/r/p", "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "user", "content": "real question about caching"}},
        {"type": "assistant", "sessionId": "s1", "cwd": "/r/p", "timestamp": "2026-01-01T00:00:02Z",
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": "caching reply"}},
    ]), encoding="utf-8")
    cx = tmp_path / "rollout-x.jsonl"
    msg = lambda t: {"timestamp": "2026-01-01T00:00:02Z", "type": "response_item",
                     "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]}}
    cx.write_text("\n".join(json.dumps(x) for x in [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": "s2", "cwd": "/r/p"}},
        msg("# AGENTS.md instructions for /r/p\n\n<INSTRUCTIONS>be terse</INSTRUCTIONS>"),
        msg("<environment_context>\n  <cwd>/r/p</cwd>\n</environment_context>"),
        msg("<external_browser_tabs>\ntab list\n</external_browser_tabs>"),
        msg("real codex question about logging"),
    ]), encoding="utf-8")
    guardian = tmp_path / "rollout-guardian.jsonl"
    guardian.write_text("\n".join(json.dumps(x) for x in [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "s3", "cwd": "/r/p", "source": {"subagent": {"other": "guardian"}}}},
        msg("Please assess the following action and replay transcript"),
    ]), encoding="utf-8")
    st = Store(tmp_path / "lore.db")
    st.ingest_file(cl, "claude")
    st.ingest_file(cx, "codex")
    assert st.ingest_file(guardian, "codex") == 0
    rows = st.con.execute("SELECT text, model FROM entries WHERE source_type='user' ORDER BY ts").fetchall()
    assert [r[0] for r in rows] == ["real question about caching", "real codex question about logging"]
    assert rows[0][1] == "claude-opus-5-5"
    assert "real codex question" in st.index("p")
    st.close()


def test_tool_output_kept_when_file_was_ingested_mid_call(tmp_path):
    p = tmp_path / "grow.jsonl"
    line = lambda d: json.dumps(d) + "\n"
    p.write_text(line({"type": "assistant", "sessionId": "g", "cwd": "/r/p", "timestamp": "t1", "message": {
        "content": [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "pytest"}}]}}), encoding="utf-8")
    st = Store(tmp_path / "lore.db")
    st.ingest_file(p, "claude")  # e.g. another session's SessionStart while this call is still running
    with open(p, "a", encoding="utf-8") as f:
        f.write(line({"type": "user", "sessionId": "g", "cwd": "/r/p", "timestamp": "t2", "message": {
            "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "42 passed"}]}}))
    st.ingest_file(p, "claude")
    assert any("pytest" in t and "42 passed" in t for (t,) in st.con.execute("SELECT text FROM entries"))
    st.close()
