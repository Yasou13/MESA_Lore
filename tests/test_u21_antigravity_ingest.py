import json
from mesa_lore.store import Store
from mesa_lore.cli import main
import os
import sys
from unittest.mock import patch


def test_antigravity_ingest_and_search(tmp_path):
    brain_dir = tmp_path / "brain" / "conv-123" / ".system_generated" / "logs"
    brain_dir.mkdir(parents=True)
    tr_file = brain_dir / "transcript.jsonl"

    lines = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "created_at": "2026-11-08T10:00:00Z",
            "content": "<user_information>\nActive Workspaces:\n- /home/dev/projects/my-proj\n</user_information>\nGümrük beyannamesi formatı nedir?"
        },
        {
            "step_index": 1,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "created_at": "2026-11-08T10:00:01Z",
            "content": "SKIP ME"
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-11-08T10:00:05Z",
            "content": "Gümrük beyannamesi CN23-TR önekiyle düzenlenecek.",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pytest -q test_customs.py"}}]
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "GENERIC",
            "created_at": "2026-11-08T10:00:08Z",
            "content": "customs ok: tarife kodu GTIP-8471 doğrulandı"
        },
        {
            "step_index": 4,
            "source": "SYSTEM",
            "type": "CHECKPOINT",
            "created_at": "2026-11-08T10:00:10Z",
            "content": "SKIP CHECKPOINT"
        },
        {
            "step_index": 5,
            "source": "SYSTEM",
            "type": "ERROR_MESSAGE",
            "created_at": "2026-11-08T10:00:12Z",
            "content": "SKIP ERROR"
        }
    ]
    tr_file.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

    db_path = tmp_path / "test.db"
    st = Store(db_path)
    try:
        first = st.ingest_file(tr_file, agent="antigravity")
        assert first == 3  # user, assistant, tool

        second = st.ingest_file(tr_file, agent="antigravity")
        assert second == 0  # dedupe

        # Search for assistant content
        r1 = st.search("gümrük beyannamesi CN23-TR", "my-proj")
        assert r1["status"] == "ok"
        assert any("CN23-TR" in x["text"] for x in r1["results"])
        assert r1["results"][0]["agent"] == "antigravity"

        # Search for tool output
        r2 = st.search("test_customs tarife kodu GTIP-8471", "my-proj")
        assert r2["status"] == "ok"
        assert any("GTIP-8471" in x["text"] for x in r2["results"])

        # System messages must not be present
        r3 = st.search("SKIP ME", "my-proj")
        assert r3["status"] == "no_match"
    finally:
        st.close()


def test_cli_ingest_antigravity(tmp_path, capsys):
    brain_dir = tmp_path / "brain" / "conv-456" / ".system_generated" / "logs"
    brain_dir.mkdir(parents=True)
    tr_file = brain_dir / "transcript.jsonl"
    tr_file.write_text(json.dumps({
        "step_index": 0,
        "type": "USER_INPUT",
        "created_at": "2026-11-08T10:00:00Z",
        "content": "<user_information>\nActive Workspaces: /home/dev/projects/cli-proj\n</user_information>\nHello CLI test."
    }) + "\n", encoding="utf-8")

    db_path = tmp_path / "cli.db"
    env = {"MESA_LORE_DB": str(db_path)}
    with patch.dict(os.environ, env):
        with patch.object(sys, "argv", ["mesa-lore", "ingest", str(tr_file), "--agent", "antigravity"]):
            main()
    captured = capsys.readouterr().out
    assert "Ingested 1 chunks" in captured or "1" in captured


def test_scan_antigravity_with_env(tmp_path, capsys):
    from mesa_lore.store import scan_antigravity
    brain_dir = tmp_path / "brain" / "conv-789" / ".system_generated" / "logs"
    brain_dir.mkdir(parents=True)
    tr_file = brain_dir / "transcript.jsonl"
    tr_file.write_text(json.dumps({
        "step_index": 0,
        "type": "USER_INPUT",
        "created_at": "2026-11-08T10:00:00Z",
        "content": "<user_information>\nActive Workspaces:\n- /home/dev/projects/scanned-proj\n</user_information>\nAuto scanned transcript."
    }) + "\n", encoding="utf-8")

    db_path = tmp_path / "scan.db"
    st = Store(db_path)
    try:
        with patch.dict(os.environ, {"ANTIGRAVITY_BRAIN_DIR": str(tmp_path / "brain")}):
            cnt = scan_antigravity(st)
            assert cnt == 1
            # dedupe check
            assert scan_antigravity(st) == 0

            # check project resolved
            r = st.search("Auto scanned", "scanned-proj")
            assert r["status"] == "ok"
    finally:
        st.close()


def test_cli_ingest_pending_scans_antigravity(tmp_path, capsys):
    brain_dir = tmp_path / "brain" / "conv-999" / ".system_generated" / "logs"
    brain_dir.mkdir(parents=True)
    tr_file = brain_dir / "transcript.jsonl"
    tr_file.write_text(json.dumps({
        "step_index": 0,
        "type": "USER_INPUT",
        "created_at": "2026-11-08T10:00:00Z",
        "content": "No workspace info here directly."
    }) + "\n", encoding="utf-8")

    db_path = tmp_path / "cli_pending.db"
    q_file = tmp_path / "queue.txt"
    env = {
        "MESA_LORE_DB": str(db_path),
        "MESA_LORE_QUEUE": str(q_file),
        "ANTIGRAVITY_BRAIN_DIR": str(tmp_path / "brain"),
    }
    with patch.dict(os.environ, env):
        with patch.object(sys, "argv", ["mesa-lore", "ingest-pending"]):
            main()
    out = capsys.readouterr().out
    assert "Ingested 1 pending chunks" in out


def test_conversation_summaries_resolution(tmp_path):
    import sqlite3
    gemini_dir = tmp_path / "gemini"
    brain_dir = gemini_dir / "brain" / "conv-summary-test" / ".system_generated" / "logs"
    brain_dir.mkdir(parents=True)
    db_file = gemini_dir / "conversation_summaries.db"
    con = sqlite3.connect(str(db_file))
    con.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, workspace_uris TEXT, project_id TEXT)")
    con.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
        ("conv-summary-test", json.dumps(["file:///home/user/code/custom-summary-proj"]), "fallback-proj")
    )
    con.commit()
    con.close()

    tr_file = brain_dir / "transcript.jsonl"
    tr_file.write_text(json.dumps({
        "step_index": 0,
        "type": "USER_INPUT",
        "created_at": "2026-11-08T10:00:00Z",
        "content": "Finding summary proj test."
    }) + "\n", encoding="utf-8")

    st = Store(tmp_path / "summary.db")
    try:
        with patch.dict(os.environ, {"ANTIGRAVITY_BRAIN_DIR": str(gemini_dir / "brain")}):
            cnt = st.ingest_file(tr_file, agent="antigravity")
            assert cnt == 1
            res = st.search("Finding summary", "custom-summary-proj")
            assert res["status"] == "ok"
    finally:
        st.close()


def test_antigravity_parallel_tool_calls_and_wrapper_stripping(tmp_path):
    brain_dir = tmp_path / "brain" / "conv-par" / ".system_generated" / "logs"
    brain_dir.mkdir(parents=True)
    tr_file = brain_dir / "transcript.jsonl"
    lines = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": "2026-11-08T10:00:00Z",
            "content": (
                "<user_information>\nActive Workspaces:\n- /home/dev/projects/par-proj\n</user_information>\n"
                "<USER_REQUEST>\nParallel tool call test question.\n</USER_REQUEST>\n"
                "<ADDITIONAL_METADATA>\nThe current local time is: 2026-11-08T10:00:00Z.\n</ADDITIONAL_METADATA>\n"
                "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Argon (Medium).\n</USER_SETTINGS_CHANGE>"
            )
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-11-08T10:00:05Z",
            "content": "Running two tools in parallel.",
            "tool_calls": [
                {"name": "read_file", "args": {"Path": "first_file.py"}},
                {"name": "read_file", "args": {"Path": "second_file.py"}}
            ]
        },
        {
            "step_index": 2,
            "type": "GENERIC",
            "created_at": "2026-11-08T10:00:06Z",
            "content": "FIRST_TOOL_OUTPUT_ALPHA"
        },
        {
            "step_index": 3,
            "type": "GENERIC",
            "created_at": "2026-11-08T10:00:07Z",
            "content": "SECOND_TOOL_OUTPUT_BETA"
        }
    ]
    tr_file.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

    st = Store(tmp_path / "par.db")
    try:
        cnt = st.ingest_file(tr_file, agent="antigravity")
        assert cnt == 4  # 1 user + 1 assistant + 2 parallel tools
        assert st.ingest_file(tr_file, agent="antigravity") == 0

        r_alpha = st.search("FIRST_TOOL_OUTPUT_ALPHA", "par-proj")
        r_beta = st.search("SECOND_TOOL_OUTPUT_BETA", "par-proj")
        assert r_alpha["status"] == "ok"
        assert r_beta["status"] == "ok"

        user_row = st.con.execute("SELECT text, model FROM entries WHERE source_type = 'user'").fetchone()
        assert user_row["text"] == "Parallel tool call test question."
        assert "<USER_REQUEST>" not in user_row["text"]
        assert "<ADDITIONAL_METADATA>" not in user_row["text"]
        assert "<USER_SETTINGS_CHANGE>" not in user_row["text"]
        assert user_row["model"] == "Gemini 4 Argon (Medium)"
    finally:
        st.close()


