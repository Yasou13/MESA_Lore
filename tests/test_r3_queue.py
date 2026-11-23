"""R3 review regression tests: end-of-session queue (no dev or holdout questions)."""
import json
import math
import os
import sys
from io import StringIO
from unittest.mock import patch

from mesa_lore import cli, mcp_server
from mesa_lore.store import Store
from mesa_lore.text import mask_secrets


def _hook(event, stdin, env):
    with patch.dict(os.environ, env), patch.object(sys, "argv", ["mesa-lore", "hook", event, "--agent", "claude"]), \
            patch("sys.stdin", StringIO(stdin)):
        cli.main()


def test_end_hook_ignores_non_json(tmp_path):
    q = tmp_path / "q.txt"
    _hook("Stop", "not json\n[*]", {"MESA_LORE_QUEUE": str(q)})
    assert not q.exists() or q.read_text() == ""


def test_ingest_pending_dedupes_and_keeps_late_entries(tmp_path, monkeypatch):
    q = tmp_path / "q.txt"
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "user", "sessionId": "s", "cwd": "/r/p", "timestamp": "2026-01-01T00:00:00Z",
                             "message": {"content": "hello"}}) + "\n", encoding="utf-8")
    q.write_text(f"{t}\n{t}\n{t}\n", encoding="utf-8")  # Stop fires every turn
    monkeypatch.setenv("MESA_LORE_QUEUE", str(q))
    st = Store(tmp_path / "lore.db")
    calls = []
    real = st.ingest_file

    def ingest(path, agent):
        calls.append(path)
        with open(q, "a", encoding="utf-8") as f:  # another session's Stop hook runs meanwhile
            f.write("/late/session.jsonl\n")
        return real(path, agent)

    monkeypatch.setattr(st, "ingest_file", ingest)
    assert cli.ingest_pending_files(st) == 1
    assert len(calls) == 1
    assert "/late/session.jsonl" in q.read_text(encoding="utf-8")
    st.close()
