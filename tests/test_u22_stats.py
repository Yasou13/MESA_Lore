from datetime import datetime, timedelta, timezone
import json
import os
import sys
from unittest.mock import patch
from mesa_lore.store import Store
from mesa_lore.cli import main


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_usage_ingest_dedupe_and_memory_detection(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    t0 = _iso(now - timedelta(days=2, hours=4))
    t1 = _iso(now - timedelta(days=2, hours=3, minutes=50))
    t2 = _iso(now - timedelta(days=2, hours=1))  # > 60 min gap (2h50m after t1) -> resume turn!

    # 1. Claude transcript: repeated message.id, 2-hour+ gap, and memory tool call
    claude_file = tmp_path / "claude_s1.jsonl"
    claude_lines = [
        {
            "type": "user", "sessionId": "cl-sess-1", "cwd": "/repo/proj-a", "timestamp": t0,
            "message": {"role": "user", "content": "Start working on proj-a"}
        },
        # First assistant turn: spans two JSONL lines with the SAME message.id (text + tool_use)
        {
            "type": "assistant", "sessionId": "cl-sess-1", "cwd": "/repo/proj-a", "timestamp": t0,
            "message": {
                "id": "msg_repeated_1", "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "Checking memory first."}],
                "usage": {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 2000,
                    "cache_read_input_tokens": 500,
                    "output_tokens": 50
                }
            }
        },
        {
            "type": "assistant", "sessionId": "cl-sess-1", "cwd": "/repo/proj-a", "timestamp": t0,
            "message": {
                "id": "msg_repeated_1", "model": "claude-opus-5-5",
                "content": [{"type": "tool_use", "id": "tu_1", "name": "mcp__mesa-lore__memory_search", "input": {"query": "auth"}}],
                "usage": {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 2000,
                    "cache_read_input_tokens": 500,
                    "output_tokens": 50
                }
            }
        },
        {
            "type": "user", "sessionId": "cl-sess-1", "cwd": "/repo/proj-a", "timestamp": t1,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}]}
        },
        # Second assistant turn (10 mins later, not a resume)
        {
            "type": "assistant", "sessionId": "cl-sess-1", "cwd": "/repo/proj-a", "timestamp": t1,
            "message": {
                "id": "msg_2", "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "Continuing."}],
                "usage": {
                    "input_tokens": 50,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 2500,
                    "output_tokens": 40
                }
            }
        },
        # Third assistant turn after a 2h50m gap -> resume turn!
        {
            "type": "assistant", "sessionId": "cl-sess-1", "cwd": "/repo/proj-a", "timestamp": t2,
            "message": {
                "id": "msg_3", "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "Resuming after 2+ hours when cache expired."}],
                "usage": {
                    "input_tokens": 200,
                    "cache_creation_input_tokens": 3000,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 60
                }
            }
        },
    ]
    claude_file.write_text("\n".join(json.dumps(x) for x in claude_lines), encoding="utf-8")

    # 2. Codex transcript: without memory, with token_count (including a duplicate total_token_usage and a 2h gap)
    t_cx0 = _iso(now - timedelta(days=10, hours=5))
    t_cx1 = _iso(now - timedelta(days=10, hours=2))  # 3h gap -> resume turn
    codex_file = tmp_path / "rollout-codex-1.jsonl"
    codex_lines = [
        {"timestamp": t_cx0, "type": "session_meta", "payload": {"id": "cx-sess-1", "cwd": "/repo/proj-a", "model": "gpt-6.1-sol"}},
        {"timestamp": t_cx0, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Fix bug"}]}},
        {
            "timestamp": t_cx0, "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 5000, "cached_input_tokens": 1000, "cache_write_input_tokens": 200, "output_tokens": 150, "total_tokens": 5350},
                    "total_token_usage": {"input_tokens": 5000, "cached_input_tokens": 1000, "cache_write_input_tokens": 200, "output_tokens": 150, "total_tokens": 5350}
                }
            }
        },
        # Duplicate token_count event with identical total_token_usage (should not be double-counted)
        {
            "timestamp": t_cx0, "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 5000, "cached_input_tokens": 1000, "cache_write_input_tokens": 200, "output_tokens": 150, "total_tokens": 5350},
                    "total_token_usage": {"input_tokens": 5000, "cached_input_tokens": 1000, "cache_write_input_tokens": 200, "output_tokens": 150, "total_tokens": 5350}
                }
            }
        },
        # Second turn 3 hours later (uses last_token_usage, NOT total_token_usage)
        {
            "timestamp": t_cx1, "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 8000, "cached_input_tokens": 2000, "cache_write_input_tokens": 300, "output_tokens": 250, "total_tokens": 8550},
                    "total_token_usage": {"input_tokens": 13000, "cached_input_tokens": 3000, "cache_write_input_tokens": 500, "output_tokens": 400, "total_tokens": 13900}
                }
            }
        },
    ]
    codex_file.write_text("\n".join(json.dumps(x) for x in codex_lines), encoding="utf-8")

    # 3. Antigravity transcript: in proj-b, with SessionStart <untrusted_memory> context
    t_ag0 = _iso(now - timedelta(days=1, hours=3))
    t_ag1 = _iso(now - timedelta(days=1, hours=1))  # 2h gap -> resume turn
    ag_dir = tmp_path / "brain" / "ag-sess-1" / ".system_generated" / "logs"
    ag_dir.mkdir(parents=True)
    ag_file = ag_dir / "transcript.jsonl"
    ag_lines = [
        {
            "step_index": 0, "type": "USER_INPUT", "created_at": t_ag0,
            "content": "<user_information>\nActive Workspaces:\n- /repo/proj-b\n</user_information>\n<untrusted_memory>\nScopes: proj-b (3)\n</untrusted_memory>\nHello ag"
        },
        {
            "step_index": 1, "type": "PLANNER_RESPONSE", "created_at": t_ag0,
            "content": "First step reply.",
            "input_tokens": 1200, "cache_read_tokens": 300, "output_tokens": 80
        },
        {
            "step_index": 2, "type": "CHECKPOINT", "created_at": t_ag0,
            "content": "checkpoint skip",
            "input_tokens": 9999, "cache_read_tokens": 9999, "output_tokens": 9999
        },
        {
            "step_index": 3, "type": "PLANNER_RESPONSE", "created_at": t_ag1,
            "content": "Second step after 2h.",
            "input_tokens": 1500, "cache_read_tokens": 0, "output_tokens": 90
        },
    ]
    ag_file.write_text("\n".join(json.dumps(x) for x in ag_lines), encoding="utf-8")

    db_path = tmp_path / "stats.db"
    st = Store(db_path)
    try:
        st.ingest_file(claude_file, "claude")
        st.ingest_file(codex_file, "codex")
        st.ingest_file(ag_file, "antigravity")

        # Re-ingest all three: must not duplicate usage rows
        st.ingest_file(claude_file, "claude")
        st.ingest_file(codex_file, "codex")
        st.ingest_file(ag_file, "antigravity")

        rows = st.con.execute(
            "SELECT session_id, agent, project, ts, uncached_input, cache_read, cache_write, output, memory_used "
            "FROM usage ORDER BY agent, ts"
        ).fetchall()

        # 3 claude + 2 codex + 2 antigravity = 7 rows
        assert len(rows) == 7

        cl_rows = [r for r in rows if r["agent"] == "claude"]
        assert len(cl_rows) == 3
        assert [r["uncached_input"] for r in cl_rows] == [100, 50, 200]
        assert [r["cache_write"] for r in cl_rows] == [2000, 100, 3000]
        assert [r["cache_read"] for r in cl_rows] == [500, 2500, 0]
        assert [r["output"] for r in cl_rows] == [50, 40, 60]
        assert all(r["memory_used"] == 1 for r in cl_rows)

        cx_rows = [r for r in rows if r["agent"] == "codex"]
        assert len(cx_rows) == 2
        # uncached_input = input_tokens - cached_input_tokens: (5000-1000=4000, 8000-2000=6000)
        assert [r["uncached_input"] for r in cx_rows] == [4000, 6000]
        assert [r["cache_read"] for r in cx_rows] == [1000, 2000]
        assert [r["cache_write"] for r in cx_rows] == [200, 300]
        assert [r["output"] for r in cx_rows] == [150, 250]
        assert all(r["memory_used"] == 0 for r in cx_rows)

        ag_rows = [r for r in rows if r["agent"] == "antigravity"]
        assert len(ag_rows) == 2
        assert [r["uncached_input"] for r in ag_rows] == [1200, 1500]
        assert [r["cache_read"] for r in ag_rows] == [300, 0]
        assert [r["cache_write"] for r in ag_rows] == [0, 0]
        assert [r["output"] for r in ag_rows] == [80, 90]
        assert all(r["memory_used"] == 1 for r in ag_rows)

        # Verify Store.stats() calculations
        data_all = st.stats()
        by_agent = {a["agent"]: a for a in data_all["agents"]}
        assert set(by_agent.keys()) == {"claude", "codex", "antigravity"}

        cl_stat = by_agent["claude"]
        assert cl_stat["sessions"] == 1
        assert cl_stat["turns"] == 3
        assert cl_stat["uncached_input"] == 350
        assert cl_stat["cache_read"] == 3000
        assert cl_stat["cache_write"] == 5100
        assert cl_stat["output"] == 150
        assert cl_stat["avg_tokens_per_turn"] == (350 + 3000 + 5100 + 150) // 3
        assert cl_stat["resume_turns"] == 1
        assert cl_stat["avg_resume_cost"] == 200 + 3000  # uncached_input + cache_write on turn 3

        cx_stat = by_agent["codex"]
        assert cx_stat["sessions"] == 1
        assert cx_stat["turns"] == 2
        assert cx_stat["resume_turns"] == 1
        assert cx_stat["avg_resume_cost"] == 6000 + 300

        # Memory comparison:
        # With memory: claude (3 turns: start cost 2100, turn2 cost 150, resume cost 3200 -> sum=5450)
        #              + antigravity (2 turns: start cost 1200, resume cost 1500 -> sum=2700)
        #              total with_memory: 2 sessions, 5 turns (sum=8150, avg=1630),
        #              starts: 2100 + 1200 = 3300 (avg=1650), resumes: 3200 + 1500 = 4700 (avg=2350)
        # Without memory: codex (1 session, 2 turns: start cost 4200, resume cost 6300 -> sum=10500, avg=5250)
        comp = data_all["memory_comparison"]
        wm = comp["with_memory"]
        wom = comp["without_memory"]
        assert wm["sessions"] == 2
        assert wm["turns"] == 5
        assert wm["avg_turn_cost"] == (2100 + 150 + 3200 + 1200 + 1500) // 5
        assert wm["avg_session_start_cost"] == (2100 + 1200) // 2
        assert wm["avg_resume_cost"] == (3200 + 1500) // 2

        assert wom["sessions"] == 1
        assert wom["turns"] == 2
        assert wom["avg_turn_cost"] == (4200 + 6300) // 2
        assert wom["avg_session_start_cost"] == 4200
        assert wom["avg_resume_cost"] == 6300

        # Filter by --project proj-a (excludes antigravity in proj-b)
        data_proj_a = st.stats(project="proj-a")
        assert {a["agent"] for a in data_proj_a["agents"]} == {"claude", "codex"}

        # Filter by --days 5 (excludes codex from 10 days ago)
        data_days_5 = st.stats(days=5)
        assert {a["agent"] for a in data_days_5["agents"]} == {"claude", "antigravity"}
    finally:
        st.close()

    # Test CLI plain-text and --json outputs
    env = {"MESA_LORE_DB": str(db_path)}
    with patch.dict(os.environ, env):
        with patch.object(sys, "argv", ["mesa-lore", "stats", "--json"]):
            from io import StringIO
            buf = StringIO()
            with patch("sys.stdout", buf):
                main()
            parsed = json.loads(buf.getvalue())
            assert "agents" in parsed and "memory_comparison" in parsed
            assert len(parsed["agents"]) == 3

        with patch.object(sys, "argv", ["mesa-lore", "stats", "--project", "proj-a", "--days", "5"]):
            buf2 = StringIO()
            with patch("sys.stdout", buf2):
                main()
            txt = buf2.getvalue()
            assert "claude" in txt
            assert "codex" not in txt
            assert "antigravity" not in txt
            assert "with memory" in txt.lower()
            assert "without memory" in txt.lower()
