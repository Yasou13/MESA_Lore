import json, os, re, sqlite3
from pathlib import Path
from urllib.parse import unquote
from mesa_lore.readers import MEMORY_TOOLS, finish, has_session_start_ctx, make_chunk, read_jsonl, tool_chunk
from mesa_lore.text import trim_tool_output

def get_antigravity_brain_dir() -> Path:
    return Path(brain_env) if (brain_env := os.environ.get("ANTIGRAVITY_BRAIN_DIR")) else Path.home() / ".gemini" / "antigravity" / "brain"

def discover_transcripts() -> list:
    return sorted(get_antigravity_brain_dir().glob("*/.system_generated/logs/transcript.jsonl"))

def parse_antigravity(path, warnings=None):
    p = Path(path)
    session_id = p.parts[p.parts.index(".system_generated") - 1] if ".system_generated" in p.parts else p.parent.name
    cwd, db_file = "", get_antigravity_brain_dir().parent / "conversation_summaries.db"
    if db_file.exists():
        try:
            con = sqlite3.connect(db_file)
            row = con.execute("SELECT workspace_uris, project_id FROM conversation_summaries WHERE conversation_id = ?", (session_id,)).fetchone()
            con.close()
            if row:
                uris, proj_id = row
                try:  # bad workspace_uris must still fall back to project_id
                    parsed = json.loads(uris) if isinstance(uris, str) else uris
                    if isinstance(parsed, list) and parsed:
                        cwd = re.sub(r"^/(?=[A-Za-z]:/)", "", unquote(parsed[0].removeprefix("file://")))
                except Exception:
                    pass
                cwd = cwd or proj_id or ""
        except Exception:
            pass

    chunks, usages, pending_tools, current_model, mem_used = [], [], [], None, False
    lines, non_empty, bad = read_jsonl(path)

    for line_no, d, _raw in lines:
        ltype = d.get("type")
        if ltype in ("SYSTEM_MESSAGE", "CHECKPOINT", "ERROR_MESSAGE"):
            continue
        ts, step_index = d.get("created_at"), d.get("step_index", line_no)
        if d.get("model"):
            current_model = d["model"]
        if ltype == "USER_INPUT":
            content = d.get("content", "")
            if has_session_start_ctx(content):
                mem_used = True
            if not cwd and (m := re.search(r"Active Workspaces:?\s*(?:\n?-\s*)?([^\s\n\r]+)", content)):
                cwd = m.group(1).strip()
            if mm := re.findall(r"changed setting `Model Selection` from .*? to (.+?)\.(?:\s|$)", content):
                current_model = mm[-1].strip()
            clean = re.sub(r"<(?:user_information|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>[\s\S]*?"
                           r"</(?:user_information|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>|</?USER_REQUEST>", "", content).strip()
            if clean:
                chunks.append(make_chunk(session_id, cwd, ts, "user", clean, f"{session_id}:{step_index}", current_model))
        elif ltype == "PLANNER_RESPONSE":
            if any(k in d for k in ("input_tokens", "cache_read_tokens", "output_tokens")):
                usages.append((session_id, cwd, ts, int(d.get("input_tokens") or 0),
                               int(d.get("cache_read_tokens") or 0), 0, int(d.get("output_tokens") or 0), f"{session_id}:u:{step_index}"))
            content = d.get("content", "")
            if isinstance(content, str) and content.strip():
                chunks.append(make_chunk(session_id, cwd, ts, "assistant", content.strip(), f"{session_id}:{step_index}", current_model))
            tcalls = d.get("tool_calls")
            if isinstance(tcalls, list):
                for sub_i, tc in enumerate(tcalls):
                    if isinstance(tc, dict):
                        name = tc.get("name", "")
                        raw_args = tc.get("args") if "args" in tc else tc.get("arguments", "")
                        tname = raw_args.get("ToolName", "") if isinstance(raw_args, dict) else ""
                        if any(t in name or t in tname for t in MEMORY_TOOLS):
                            mem_used = True
                        args = json.dumps(raw_args, ensure_ascii=False) if isinstance(raw_args, dict) else str(raw_args)
                        pending_tools.append({"id": tc.get("id") or tc.get("call_id"), "name": name, "args": trim_tool_output(args.strip()),
                                              "ts": ts, "step": f"{step_index}:{sub_i}", "model": current_model})
        elif ltype == "GENERIC":
            content = d.get("content", "")
            cid = d.get("tool_call_id") or d.get("call_id") or d.get("toolCallId") or d.get("id")
            match = next((i for i, pt in enumerate(pending_tools) if cid and pt["id"] == cid), None)
            if match is None and not any(d.get(k) for k in ("tool_call_id", "call_id", "toolCallId")):
                match = 0 if pending_tools else None
            elif match is None and pending_tools and all(pt["id"] is None for pt in pending_tools):
                match = 0
            tc = pending_tools.pop(match) if match is not None else None
            if content or tc:
                call = tc or {}
                suffix = f"{call['step']}:tool" if tc else step_index
                chunks.append(tool_chunk(session_id, cwd, call.get("ts", ts), f"{session_id}:{suffix}",
                                         call.get("model") or current_model, call.get("name", ""),
                                         call.get("args", ""), content, d.get("is_error") is True))
    return finish(chunks, usages, mem_used, warnings, non_empty, bad)
