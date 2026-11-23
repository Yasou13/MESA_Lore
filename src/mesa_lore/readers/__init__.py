import json

MEMORY_TOOLS = ("memory_search", "memory_get", "memory_index", "memory_save")


def has_session_start_ctx(s: str, keys=("Scopes:", "Notes:", "Handoffs:", "Recent sessions:", "SessionStart")) -> bool:
    return "<untrusted_memory>" in s and any(k in s for k in keys)


def read_jsonl(path) -> tuple:
    """Read non-empty JSONL lines; return (parsed (line_no, dict, raw), non_empty_count, bad_count)."""
    items, non_empty, bad = [], 0, 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line_no, raw in enumerate(f):
            if not (raw := raw.strip()):
                continue
            non_empty += 1
            try:
                d = json.loads(raw)
            except Exception:
                d = None
            if isinstance(d, dict):
                items.append((line_no, d, raw))
            else:
                bad += 1
    return items, non_empty, bad


def make_chunk(session_id: str, cwd: str, ts, role: str, text: str, dedupe_key: str, model) -> dict:
    return {"session_id": session_id, "cwd": cwd, "ts": ts, "role": role, "text": text, "dedupe_key": dedupe_key, "model": model}


def make_usage(session_id: str, cwd: str, ts, uncached: int, c_read: int, c_write: int, out: int, mem_used: int, dedupe_key: str) -> dict:
    return {"session_id": session_id, "cwd": cwd, "ts": ts, "uncached_input": uncached, "cache_read": c_read,
            "cache_write": c_write, "output": out, "memory_used": mem_used, "dedupe_key": dedupe_key}


def collect_warning(warnings, non_empty: int, bad: int, chunks: list, usages: list):
    if warnings is None or non_empty == 0:
        return
    if bad * 2 > non_empty:
        warnings.append(f"unrecognized/corrupt format: {bad}/{non_empty} bad lines")
    elif not chunks and not usages:
        warnings.append(f"unrecognized format: 0 chunks from {non_empty} lines")


from mesa_lore.readers.antigravity import parse_antigravity  # noqa: E402
from mesa_lore.readers.claude import parse_claude  # noqa: E402
from mesa_lore.readers.codex import parse_codex  # noqa: E402

READERS = {"claude": parse_claude, "codex": parse_codex, "antigravity": parse_antigravity}


def parse_transcript(path, agent: str, warnings=None):
    reader = READERS.get(agent)
    return reader(path, warnings=warnings) if reader else ([], [])
