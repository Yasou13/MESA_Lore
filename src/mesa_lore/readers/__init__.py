import json, re
from pathlib import Path

from mesa_lore.text import trim_tool_output

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
                if isinstance(d := json.loads(raw), dict):
                    items.append((line_no, d, raw))
                    continue
            except ValueError:
                pass
            bad += 1
    return items, non_empty, bad

def make_chunk(session_id: str, cwd: str, ts, role: str, text: str, dedupe_key: str, model) -> dict:
    return {"session_id": session_id, "cwd": cwd, "ts": ts, "role": role, "text": text, "dedupe_key": dedupe_key, "model": model}

_TOOL_PATH = re.compile(r'"(?:file_path|filePath|path|TargetFile|Path)"\s*:\s*("(?:\\.|[^"\\])*")|(?:^|\\n)\*\*\* (?:Update|Add|Delete|Move to) File:[ \t]*(.+?)(?=\\n|$)', re.M)
_EXIT = re.compile(r"(?m)^(?:(?:Process )?exited with code|Exit code:)\s*-?[1-9]\d*\b", re.I)

def tool_chunk(sid, cwd, ts, pos, model, name, args, result, flagged=False):
    output = json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result or "")
    trimmed = trim_tool_output(output.strip(), True)
    chunk = make_chunk(sid, cwd, ts, "tool", f"{name} {args}\n{trimmed}".strip(), pos, model)
    chunk["paths"] = [re.sub(r"\\(.)", r"\1", path[1:-1]) if path else patch.strip() for path, patch in _TOOL_PATH.findall(args)]
    tool = name.lower().rsplit("__", 1)[-1]
    if tool in ("exec", "exec_command", "bash", "shell"):
        chunk["paths"].extend(p for p in re.findall(r"""(?<![\w:/])(?:[A-Za-z]:)?/[^\s"'`<>()\[\]{};|,]+""", args) if Path(p).is_file())
    chunk["changed"] = bool(re.search(r"edit|write|replace|patch|delete", tool))
    code = result.get("exit_code", 0) if isinstance(result, dict) else 0
    if flagged or (not chunk["changed"] and tool not in ("read", "read_file", "view_image")
                   and (code or _EXIT.search("\n".join(trimmed.splitlines()[:4])))):
        error_lines = [line.strip() for line in trimmed.splitlines() if line.strip()]
        chunk["error"] = next((line for line in reversed(error_lines) if re.match(r"\w+(?:Error|Exception):", line)), next(iter(error_lines), "Tool failed"))
    return chunk

def finish(chunks: list, usages: list, mem_used, warnings, non_empty: int, bad: int, warn=True) -> tuple:
    """A reader's result. usages: (session_id, cwd, ts, uncached, cache_read, cache_write, output, dedupe_key)."""
    keys = ("session_id", "cwd", "ts", "uncached_input", "cache_read", "cache_write", "output", "memory_used", "dedupe_key")
    out = [dict(zip(keys, (*u[:7], int(mem_used), u[7]))) for u in usages]
    if warn and warnings is not None and non_empty:
        if bad * 2 > non_empty:
            warnings.append(f"unrecognized/corrupt format: {bad}/{non_empty} bad lines")
        elif not chunks and not out:
            warnings.append(f"unrecognized format: 0 chunks from {non_empty} lines")
    return chunks, out

from mesa_lore.readers.antigravity import parse_antigravity  # noqa: E402
from mesa_lore.readers.claude import parse_claude  # noqa: E402
from mesa_lore.readers.codex import parse_codex  # noqa: E402

READERS = {"claude": parse_claude, "codex": parse_codex, "antigravity": parse_antigravity}

def parse_transcript(path, agent: str, warnings=None):
    reader = READERS.get(agent)
    return reader(path, warnings=warnings) if reader else ([], [])
