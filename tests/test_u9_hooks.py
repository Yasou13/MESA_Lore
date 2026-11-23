import json
import os
import sys
from io import StringIO
from unittest.mock import patch
from mesa_lore.cli import main
from mesa_lore.store import Store


def run_cli_with_stdin(args, env, stdin_content, capsys):
    with patch.dict(os.environ, env):
        with patch.object(sys, "argv", ["mesa-lore"] + args):
            with patch("sys.stdin", StringIO(stdin_content)):
                main()
    return capsys.readouterr()


def test_hook_session_end_claude(tmp_path, capsys):
    queue_path = tmp_path / "queue.txt"
    env = {"MESA_LORE_QUEUE": str(queue_path)}

    payload = json.dumps({"transcript_path": "/tmp/test_session.jsonl"})
    out = run_cli_with_stdin(["hook", "SessionEnd", "--agent", "claude"], env, payload, capsys)

    assert queue_path.exists()
    assert "/tmp/test_session.jsonl" in queue_path.read_text(encoding="utf-8")


def test_hook_stop_codex(tmp_path, capsys):
    queue_path = tmp_path / "queue.txt"
    env = {"MESA_LORE_QUEUE": str(queue_path)}

    payload = json.dumps({"session_id": "sess-xyz-789"})
    out = run_cli_with_stdin(["hook", "Stop", "--agent", "codex"], env, payload, capsys)

    assert "sess-xyz-789" in queue_path.read_text(encoding="utf-8")


def test_hook_session_start_ingests_and_prints_index(tmp_path, capsys):
    db_path = tmp_path / "lore.db"
    queue_path = tmp_path / "queue.txt"
    env = {"MESA_LORE_DB": str(db_path), "MESA_LORE_QUEUE": str(queue_path)}

    # Create a transcript file to be ingested
    transcript = tmp_path / "t1.jsonl"
    transcript.write_text(
        '{"type": "user", "sessionId": "s9", "cwd": "/repo/sample-proj", "timestamp": "2026-01-01T10:00:00Z", "message": {"content": "Hello in sample-proj"}}\n',
        encoding="utf-8"
    )
    # Preload queue
    queue_path.write_text(str(transcript) + "\n", encoding="utf-8")

    # Save an active note for sample-proj
    st = Store(db_path)
    st.save_note("sample-proj", "Architecture: Use Postgres", "decision")
    st.close()

    payload = json.dumps({"cwd": "/repo/sample-proj"})
    out = run_cli_with_stdin(["hook", "SessionStart", "--agent", "claude"], env, payload, capsys)

    # Queue should be empty now
    assert queue_path.read_text(encoding="utf-8").strip() == ""

    # Output should contain index
    assert "Architecture: Use Postgres" in out.out
    assert "Hello in sample-proj" in out.out


def test_hook_all_events_claude_and_codex_and_rollout_resolution(tmp_path, capsys):
    import subprocess
    import time

    fake_home = tmp_path / "home"
    codex_dir = fake_home / ".codex" / "sessions" / "2026" / "11" / "08"
    codex_dir.mkdir(parents=True)
    rollout_file = codex_dir / "rollout-2026-11-08T10-00-00-019f5f6d-b3d0-73f0.jsonl"
    rollout_file.write_text(
        json.dumps({"timestamp": "2026-11-08T10:00:00Z", "type": "session_meta", "payload": {"id": "019f5f6d-b3d0-73f0", "cwd": "/repo/codex-proj"}}) + "\n" +
        json.dumps({"timestamp": "2026-11-08T10:00:05Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Codex rollout question"}]}}) + "\n",
        encoding="utf-8"
    )

    claude_file = tmp_path / "claude_sess.jsonl"
    claude_file.write_text(
        json.dumps({"type": "user", "sessionId": "cl-1", "cwd": "/repo/claude-proj", "timestamp": "2026-11-08T10:00:00Z", "message": {"role": "user", "content": "Claude session question"}}) + "\n",
        encoding="utf-8"
    )

    db_path = tmp_path / "hooks.db"
    queue_path = tmp_path / "queue.txt"
    env = {
        **os.environ,
        "HOME": str(fake_home),
        "MESA_LORE_DB": str(db_path),
        "MESA_LORE_QUEUE": str(queue_path),
    }

    # 1. End-of-session hooks (Claude Stop & SessionEnd, Codex Stop & SessionEnd) via real subprocess:
    # must finish well under 1 second and only enqueue (never create db_path).
    end_events = [
        ("claude", "Stop", {"session_id": "cl-1", "transcript_path": str(claude_file), "cwd": "/repo/claude-proj", "hook_event_name": "Stop", "stop_hook_active": False}),
        ("claude", "SessionEnd", {"session_id": "cl-1", "transcript_path": str(claude_file), "cwd": "/repo/claude-proj", "hook_event_name": "SessionEnd", "reason": "exit"}),
        ("codex", "Stop", {"session_id": "019f5f6d-b3d0-73f0", "cwd": "/repo/codex-proj", "hook_event_name": "Stop"}),
        ("codex", "SessionEnd", {"session_id": "019f5f6d-b3d0-73f0", "cwd": "/repo/codex-proj", "hook_event_name": "SessionEnd"}),
    ]
    for agent, ev, payload in end_events:
        t0 = time.perf_counter()
        res = subprocess.run(
            [sys.executable, "-m", "mesa_lore.cli", "hook", ev, "--agent", agent],
            input=json.dumps(payload),
            text=True,
            env=env,
            capture_output=True,
            check=True,
        )
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, f"{agent} {ev} took {elapsed:.3f}s (>= 1.0s)"
        assert not db_path.exists(), f"{agent} {ev} should only enqueue, not touch DB"

    queued = queue_path.read_text(encoding="utf-8").splitlines()
    assert str(claude_file) in queued
    assert "019f5f6d-b3d0-73f0" in queued

    # 2. SessionStart for Codex resolves session_id -> rollout-...-<session_id>.jsonl and prints index
    out_codex = run_cli_with_stdin(
        ["hook", "SessionStart", "--agent", "codex"],
        env,
        json.dumps({"session_id": "new-cx", "cwd": "/repo/codex-proj", "hook_event_name": "SessionStart"}),
        capsys,
    )
    assert "Codex rollout question" in out_codex.out

    # 3. SessionStart for Claude prints index for claude-proj
    out_claude = run_cli_with_stdin(
        ["hook", "SessionStart", "--agent", "claude"],
        env,
        json.dumps({"session_id": "new-cl", "cwd": "/repo/claude-proj", "hook_event_name": "SessionStart"}),
        capsys,
    )
    assert "Claude session question" in out_claude.out
