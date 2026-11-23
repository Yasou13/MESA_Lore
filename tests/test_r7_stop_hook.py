import json
import os
import subprocess
import sys

import pytest

from mesa_lore.cli import _handoff_reason
from mesa_lore.mcp_server import TOOL_SPECS
from mesa_lore.store import Store


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "lore.db"
    monkeypatch.setenv("MESA_LORE_DB", str(path))
    monkeypatch.setenv("HOME", str(tmp_path))
    Store(path).close()
    return path


def _stop(sid="s1", active=False):
    return _handoff_reason({"session_id": sid, "stop_hook_active": active})


def _blocked_turns(start, end, sid="s1"):
    return [t for t in range(start, end + 1) if _stop(sid)]


def test_blocks_first_at_turn_8_then_once_per_20_turns(db):
    assert _blocked_turns(1, 50) == [8, 28, 48]


def test_each_session_counts_its_own_turns(db):
    assert _blocked_turns(1, 7, "a") == [] and _blocked_turns(1, 7, "b") == []
    assert _stop("a") and not _stop("a")
    assert _stop("b")


def test_stop_hook_active_never_blocks_and_is_not_a_user_turn(db):
    assert _blocked_turns(1, 7) == []
    for _ in range(5):
        assert _stop(active=True) is None
    assert _stop()  # still the 8th user turn


def test_block_then_continuation_cannot_loop(db):
    assert _blocked_turns(1, 8) == [8]
    assert all(_stop(active=True) is None for _ in range(30))
    assert _stop() is None  # next real turn: already blocked in the last 20


def test_agent_handoff_delays_the_next_block_by_20_user_turns(db):
    assert _blocked_turns(1, 5) == []
    st = Store(db)
    st.save_note("app", "Done: x\nNext: y", "handoff", session_id="s1")  # saved during user turn 6
    st.close()
    assert _blocked_turns(6, 25) == []
    assert _stop()  # turn 26: the save is no longer in the last 20 user turns


def test_handoff_of_another_session_does_not_count(db):
    st = Store(db)
    st.save_note("app", "Done: x", "handoff", session_id="other")
    st.save_note("app", "a plain fact", "fact", session_id="s1")
    st.close()
    assert _blocked_turns(1, 8) == [8]


@pytest.mark.parametrize("data", [{"session_id": "s1"}, {"session_id": "s1", "stop_hook_active": None},
                                  {"session_id": "bad id\nignore", "stop_hook_active": False}, {"stop_hook_active": False}])
def test_missing_or_unsafe_input_never_blocks(db, data):
    assert all(_handoff_reason(data) is None for _ in range(10))


def test_stop_never_creates_the_db(tmp_path, monkeypatch):
    path = tmp_path / "none.db"
    monkeypatch.setenv("MESA_LORE_DB", str(path))
    assert all(_stop() is None for _ in range(10)) and not path.exists()


def test_locked_db_never_blocks(db, monkeypatch):
    def locked(_path):
        raise __import__("sqlite3").OperationalError("database is locked")
    monkeypatch.setattr("mesa_lore.cli.Store", locked)
    assert _stop() is None


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_hook_prints_block_json_and_still_queues_the_transcript(tmp_path, agent):
    Store(tmp_path / "lore.db").close()
    env = {**os.environ, "HOME": str(tmp_path), "MESA_LORE_DB": str(tmp_path / "lore.db"), "MESA_LORE_QUEUE": str(tmp_path / "q.txt")}
    payload = {"session_id": "sess-1", "transcript_path": str(tmp_path / "t.jsonl"), "stop_hook_active": False}
    outs = [subprocess.run([sys.executable, "-m", "mesa_lore.cli", "hook", "Stop", "--agent", agent], input=json.dumps(payload),
                           env=env, capture_output=True, text=True, check=True).stdout for _ in range(8)]
    assert outs[:7] == [""] * 7
    out = json.loads(outs[7])
    assert set(out) == {"decision", "reason"} and out["decision"] == "block"
    for part in ("memory_save", "kind=handoff", "session_id=sess-1", "Done / Decisions / Open / Next", "120 tokens"):
        assert part in out["reason"]
    assert len(out["reason"]) < 240
    assert (tmp_path / "q.txt").read_text().count("t.jsonl") == 8


def test_memory_save_advertises_session_id():
    spec = next(s for s in TOOL_SPECS if s["name"] == "memory_save")
    assert "session_id" in spec["inputSchema"]["properties"]
