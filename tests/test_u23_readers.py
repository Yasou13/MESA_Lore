import inspect
from pathlib import Path

from mesa_lore import store
from mesa_lore.readers import parse_transcript
from mesa_lore.readers.antigravity import parse_antigravity
from mesa_lore.readers.claude import parse_claude
from mesa_lore.readers.codex import parse_codex

FIX = Path(__file__).resolve().parent.parent / "bench" / "fixtures"
CHUNK_KEYS = {"session_id", "cwd", "ts", "role", "text", "dedupe_key", "model"}
USAGE_KEYS = {
    "session_id", "cwd", "ts", "uncached_input", "cache_read",
    "cache_write", "output", "memory_used", "dedupe_key",
}


def test_readers_emit_unified_record_format():
    cases = [
        (parse_claude, FIX / "claude" / "a1.jsonl"),
        (parse_codex, FIX / "codex" / "c1.jsonl"),
        (parse_antigravity, FIX / "antigravity" / "ag1" / ".system_generated" / "logs" / "transcript.jsonl"),
    ]
    for fn, path in cases:
        chunks, usages = fn(path)
        assert len(chunks) > 0
        for c in chunks:
            extras = set(c) - CHUNK_KEYS
            assert CHUNK_KEYS <= set(c)
            if c["role"] == "tool":
                assert {"paths", "changed"} <= extras <= {"paths", "changed", "error"}
            else:
                assert not extras
            assert c["role"] in ("user", "assistant", "tool")
            assert isinstance(c["text"], str) and c["text"]
        for u in usages:
            assert set(u.keys()) == USAGE_KEYS


def test_parse_transcript_dispatch():
    chunks, _ = parse_transcript(FIX / "codex" / "c1.jsonl", "codex")
    assert any("KRG-" in c["text"] for c in chunks)
    assert parse_transcript(FIX / "codex" / "c1.jsonl", "unknown") == ([], [])


def test_store_does_not_know_raw_transcript_formats():
    src = inspect.getsource(store)
    for raw_marker in ("PLANNER_RESPONSE", "response_item", "function_call_output", "tool_result"):
        assert raw_marker not in src
