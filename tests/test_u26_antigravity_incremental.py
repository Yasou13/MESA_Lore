import json
from mesa_lore.readers.antigravity import parse_antigravity
import mesa_lore.store as store_mod
from mesa_lore.store import SCHEMA_VERSION, Store, scan_antigravity


def test_incremental_skip_unchanged_and_reingest_on_append(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    conv = brain / "conv-u26" / ".system_generated" / "logs"
    conv.mkdir(parents=True)
    tf = conv / "transcript.jsonl"
    tf.write_text(json.dumps({
        "type": "USER_INPUT", "step_index": 0, "created_at": "2026-11-10T10:00:00Z",
        "content": "<user_information>\nActive Workspaces:\n- /tmp/app1\n</user_information>\nFirst message",
    }) + "\n", encoding="utf-8")

    monkeypatch.setenv("ANTIGRAVITY_BRAIN_DIR", str(brain))
    st = Store(tmp_path / "lore.db")
    try:
        calls = 0
        orig = store_mod.parse_transcript

        def spy(path, agent, warnings=None):
            nonlocal calls
            calls += 1
            return orig(path, agent, warnings=warnings)

        monkeypatch.setattr(store_mod, "parse_transcript", spy)

        assert scan_antigravity(st) == 1
        assert calls == 1
        assert st.con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        assert st.con.execute("SELECT count(*) FROM ingested_files").fetchone()[0] == 1

        # Second scan when file is unchanged must not re-parse
        assert scan_antigravity(st) == 0
        assert calls == 1

        # Append a new step -> size/mtime change triggers incremental re-ingest
        with open(tf, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "PLANNER_RESPONSE", "step_index": 1, "created_at": "2026-11-10T10:00:05Z",
                "content": "Second message from planner",
            }) + "\n")
        assert scan_antigravity(st) == 1
        assert calls == 2
        assert st.search("Second message", "app1")["status"] == "ok"
    finally:
        st.close()


def test_out_of_order_tool_pairing_by_call_id_and_fifo_fallback(tmp_path):
    tf = tmp_path / "transcript.jsonl"
    tf.write_text("\n".join([
        json.dumps({
            "type": "PLANNER_RESPONSE", "step_index": 1, "created_at": "2026-11-10T10:00:01Z",
            "tool_calls": [
                {"id": "call_a", "name": "read_file", "args": {"Path": "/tmp/a.py"}},
                {"id": "call_b", "name": "run_command", "args": {"CommandLine": "pytest"}},
            ],
        }),
        json.dumps({"type": "GENERIC", "step_index": 2, "tool_call_id": "call_b", "content": "3 passed"}),
        json.dumps({"type": "GENERIC", "step_index": 3, "tool_call_id": "call_a", "content": "def hello(): pass"}),
        # Legacy tool calls without IDs -> FIFO fallback
        json.dumps({
            "type": "PLANNER_RESPONSE", "step_index": 4, "created_at": "2026-11-10T10:00:02Z",
            "tool_calls": [
                {"name": "tool_one", "args": "arg1"},
                {"name": "tool_two", "args": "arg2"},
            ],
        }),
        json.dumps({"type": "GENERIC", "step_index": 5, "content": "out_one"}),
        json.dumps({"type": "GENERIC", "step_index": 6, "content": "out_two"}),
    ]) + "\n", encoding="utf-8")

    chunks, _ = parse_antigravity(tf)
    tools = [c["text"] for c in chunks if c["role"] == "tool"]
    assert any("run_command" in t and "3 passed" in t and "read_file" not in t for t in tools)
    assert any("read_file" in t and "def hello()" in t and "run_command" not in t for t in tools)
    assert any("tool_one" in t and "out_one" in t for t in tools)
    assert any("tool_two" in t and "out_two" in t for t in tools)
