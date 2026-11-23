import json, os, re, sqlite3
from pathlib import Path
from urllib.parse import unquote
from mesa_lore.readers import MEMORY_TOOLS, collect_warning, has_session_start_ctx, make_chunk, make_usage, read_jsonl
from mesa_lore.text import trim_tool_output


def get_antigravity_brain_dir() -> Path:
    brain_env = os.environ.get("ANTIGRAVITY_BRAIN_DIR")
    return Path(brain_env) if brain_env else Path.home() / ".gemini" / "antigravity" / "brain"


def discover_transcripts() -> list:
    ag_brain = get_antigravity_brain_dir()
    return sorted(ag_brain.glob("*/.system_generated/logs/transcript.jsonl")) if ag_brain.exists() else []


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
                    cwd = unquote(parsed[0].removeprefix("file://")) if isinstance(parsed, list) and parsed else ""
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
            content = json.dumps(content, ensure_ascii=False) if isinstance(content, (dict, list)) else str(content)
            trimmed_out = trim_tool_output(content.strip(), True)
            cid = d.get("tool_call_id") or d.get("call_id") or d.get("toolCallId") or d.get("id")
            tc = None
            if cid:
                for i, pt in enumerate(pending_tools):
                    if pt.get("id") == cid:
                        tc = pending_tools.pop(i)
                        break
            if tc is None and pending_tools:
                has_explicit = bool(d.get("tool_call_id") or d.get("call_id") or d.get("toolCallId"))
                if not has_explicit or all(pt.get("id") is None for pt in pending_tools):
                    for i, pt in enumerate(pending_tools):
                        if pt.get("id") is None or not has_explicit:
                            tc = pending_tools.pop(i)
                            break
            if tc is not None:
                merged = f"{tc['name']} {tc['args']}\n{trimmed_out}".strip()
                chunks.append(make_chunk(session_id, cwd, tc["ts"], "tool", merged, f"{session_id}:{tc['step']}:tool", tc.get("model") or current_model))
            elif trimmed_out:
                chunks.append(make_chunk(session_id, cwd, ts, "tool", trimmed_out, f"{session_id}:{step_index}", current_model))

    out_usages = [make_usage(s, cw, t, ui, cr, cb, out, int(mem_used), dk) for s, cw, t, ui, cr, cb, out, dk in usages]
    collect_warning(warnings, non_empty, bad, chunks, out_usages)
    return chunks, out_usages
