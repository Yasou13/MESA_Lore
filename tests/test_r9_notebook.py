import json
import math
import subprocess

from mesa_lore.store import Store


def _claude(path, cwd, sid, turns):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps({
        "type": role, "sessionId": sid, "cwd": str(cwd),
        "timestamp": f"2026-11-23T10:00:{i:02d}Z",
        "message": {"role": role, "content": content},
    }) for i, (role, content) in enumerate(turns)), encoding="utf-8")
    return path


def _tool(name, tid, args, output, **result):
    return [
        ("assistant", [{"type": "tool_use", "id": tid, "name": name, "input": args}]),
        ("user", [{"type": "tool_result", "tool_use_id": tid, "content": output, **result}]),
    ]


def _repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_scope_uses_touched_repository_or_misc(tmp_path):
    repo = _repo(tmp_path / "real-project")
    outside = tmp_path / "pro"
    outside.mkdir()
    file = repo / "settings.py"
    file.touch()
    touched = _claude(tmp_path / "touched.jsonl", outside, "touch", [
        ("user", "Update settings."),
        *_tool("Edit", "edit", {"file_path": str(file)}, "ok"),
    ])
    loose = _claude(tmp_path / "loose.jsonl", outside, "loose", [("user", "Read this draft.")])
    st = Store(tmp_path / "round.db")
    try:
        st.ingest_file(touched, "claude")
        st.ingest_file(loose, "claude")
        assert {tuple(r) for r in st.con.execute("SELECT DISTINCT session_id, project FROM entries")} == {
            ("touch", "real-project"), ("loose", "misc"),
        }
    finally:
        st.close()


def test_handoff_uses_first_request_last_reply_and_actual_error_results(tmp_path):
    repo = _repo(tmp_path / "app")
    first = "Fix request " + "detail " * 20
    source = _claude(tmp_path / "real.jsonl", repo, "real", [
        ("user", first),
        *_tool("Read", "read", {"file_path": "src/other.py"}, "Traceback code fixture\nValueError: fake"),
        *_tool("Edit", "edit", {"file_path": str(repo / "src/main.py")}, "ok\nValueError: example"),
        *_tool("Edit", "temporary", {"file_path": "/tmp/external.py"}, "ok"),
        *_tool("Bash", "pass", {"command": "run"}, "Exit code: 0\nFAILED is a string"),
        *_tool("Bash", "fail", {"command": "run"}, "ValueError: actual", is_error=True),
        ("assistant", "Earlier report."),
        ("user", "Later request."),
        ("assistant", "First. Second. Third sentence."),
    ])
    st = Store(tmp_path / "round.db")
    try:
        st.ingest_file(source, "claude")
        st.save_note("app", "Run final check", "todo")
        st.con.execute("DELETE FROM ingested_files")
        st.ingest_file(source, "claude")
        text = st.con.execute("SELECT text FROM handoffs").fetchone()[0]
        lines = text.splitlines()
        assert len(lines) == 5 and math.ceil(len(text) / 4) <= 120
        assert lines[0].startswith("Asked: Fix request") and len(lines[0].removeprefix("Asked: ")) <= 100
        assert lines[1:] == [
            "Done: First. Second.", "Files: src/main.py", "Errors: ValueError: actual",
            "Open: Run final check",
        ]
    finally:
        st.close()


def test_evaluators_and_subagents_are_not_notebook_requests(tmp_path):
    repo = _repo(tmp_path / "app")
    main = _claude(tmp_path / "parent.jsonl", repo, "parent", [("user", "Deploy preview.")])
    side = _claude(tmp_path / "parent/subagents/agent-side.jsonl", repo, "parent", [
        ("user", "Subagent-only request."), ("assistant", "Subagent-only reply."),
    ])
    eval_source = _claude(tmp_path / "eval.jsonl", repo, "eval", [
        ("user", "You are the ANSWERING model in a retrieval evaluation."),
        ("assistant", "Benchmark-only reply."),
    ])
    st = Store(tmp_path / "round.db")
    try:
        for file in (main, side, eval_source):
            st.ingest_file(file, "claude")
        for output in (st.index("app"), st.export("app")):
            assert "Deploy preview." in output
            assert all(marker not in output for marker in ("Subagent-only", "ANSWERING model", "Benchmark-only"))
    finally:
        st.close()


def test_export_has_three_leading_sections_and_latest_30_multiline_sessions(tmp_path):
    st = Store(tmp_path / "round.db")
    try:
        st.save_note("app", "Verify documentation", "todo")
        st.save_note("app", "Use SQLite", "decision")
        with st.con:
            for i in range(32):
                st.con.execute(
                    "INSERT INTO handoffs (id, project, text, session_id, ts, agent) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(i), "app", f"Asked: Request {i:02d}\nDone: Result {i:02d}", str(i),
                     f"2026-11-{20 + i // 16}T10:{i % 16:02d}:00Z", "codex"),
                )
        text = st.export("app")
        assert [line for line in text.splitlines() if line.startswith("## ")] == [
            "## Open items", "## Decisions", "## Sessions",
        ]
        assert text.count("### 2026-11-21") == text.count("### 2026-11-20") == 1
        assert text.index("### 2026-11-21") < text.index("### 2026-11-20")
        assert text.count("Asked: ") == 30
        assert "Asked: Request 31\nDone: Result 31" in text and "codex" in text
        assert "Asked: Request 02" in text and "Asked: Request 01" not in text
    finally:
        st.close()


def test_scope_can_use_an_existing_file_in_codex_exec_arguments(tmp_path):
    repo = _repo(tmp_path / "actual-project")
    outside = tmp_path / "tools"
    outside.mkdir()
    source = tmp_path / "rollout.jsonl"
    touched = repo / "settings.py"
    touched.touch()
    events = [
        {"type": "session_meta", "payload": {"id": "exec", "cwd": str(outside)}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"text": "Check settings."}]}},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "call",
            "input": f'await tools.exec_command({{cmd: "cat {touched.as_posix()}"}})',
        }},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "call", "output": "contents",
        }},
    ]
    source.write_text("\n".join(map(json.dumps, events)), encoding="utf-8")
    st = Store(tmp_path / "round.db")
    try:
        st.ingest_file(source, "codex")
        assert {row[0] for row in st.con.execute("SELECT DISTINCT project FROM entries")} == {"actual-project"}
    finally:
        st.close()


def test_legacy_assistant_only_evaluation_receipts_are_excluded(tmp_path):
    st = Store(tmp_path / "round.db")
    try:
        for n, receipt in enumerate(("done (judge_out_00.json: 20)", "done ans_out_01.json: 20",
                                     "Blind judging results written to /tmp/delta_sec_out_4c.jsonl")):
            with st.con:
                st.con.execute(
                    "INSERT INTO handoffs (id, project, text, session_id, ts, agent) VALUES (?, ?, ?, ?, ?, ?)",
                    (f"eval{n}", "app", f"Yapılan: {receipt}", f"eval{n}", "2026-11-23T10:00:00Z", "antigravity"),
                )
        for output in (st.index("app"), st.export("app")):
            assert all(marker not in output for marker in ("ans_out_", "judge_out_", "delta_sec_out_"))
    finally:
        st.close()
