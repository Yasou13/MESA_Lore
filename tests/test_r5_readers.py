import json
import sqlite3
from mesa_lore.readers.antigravity import parse_antigravity


def test_antigravity_bad_workspace_uris_falls_back_to_project_id(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    logs = brain / "conv-1" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    con = sqlite3.connect(tmp_path / "conversation_summaries.db")
    con.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, workspace_uris TEXT, project_id TEXT)")
    con.execute("INSERT INTO conversation_summaries VALUES ('conv-1', '{not json', '/repo/fallback-app')")
    con.commit()
    con.close()
    tf = logs / "transcript.jsonl"
    tf.write_text(json.dumps({"type": "USER_INPUT", "step_index": 0, "created_at": "2026-11-15T12:00:00Z", "content": "hello there"}) + "\n")
    monkeypatch.setenv("ANTIGRAVITY_BRAIN_DIR", str(brain))
    chunks = parse_antigravity(tf)[0]
    assert chunks and chunks[0]["cwd"] == "/repo/fallback-app"
