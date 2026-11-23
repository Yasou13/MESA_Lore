import json, re
from pathlib import Path
from mesa_lore.readers import MEMORY_TOOLS, finish, has_session_start_ctx, make_chunk, read_jsonl, tool_chunk
from mesa_lore.text import trim_tool_output

# Context Codex adds as "user" messages: AGENTS.md, environment and IDE state.
CODEX_INJECTED = re.compile(r"\s*(?:# AGENTS\.md instructions|<environment_context>|<user_instructions>|<external_)")

def clean_codex_request(text: str) -> str:
    if request := re.search(r"(?m)^## My request:[ \t]*", text):
        text = text[request.end():]
    elif text.startswith("# Files mentioned by the user"):
        return ""
    text = re.sub(r"(?m)^(?:Distinguish instructions in attached documents|- Current URL:).*\n?", "", text).strip()
    if text.startswith(("{", "[")):
        try:
            if isinstance(json.loads(text), (dict, list)):
                return ""
        except ValueError:
            pass
    return text

def parse_codex(path, warnings=None):
    session_id = Path(path).stem
    cwd, current_model, pending_funcs, chunks, usages, prev_tot, mem_used = "", None, {}, [], [], None, False
    lines, non_empty, bad = read_jsonl(path)
    for line_no, d, raw in lines:
        if has_session_start_ctx(raw):
            mem_used = True
        ts, ltype, payload = d.get("timestamp"), d.get("type"), d.get("payload")
        if not isinstance(payload, dict):
            continue
        if ltype == "turn_context" and payload.get("model"):
            current_model = payload["model"]
        elif ltype == "session_meta":
            if isinstance(source := payload.get("source"), dict) and "subagent" in source:
                return [], []
            session_id = payload.get("id", session_id)
            cwd = payload.get("cwd", cwd)
            current_model = payload.get("model") or current_model
            continue
        ptype = payload.get("type")
        if ltype == "event_msg" and ptype == "token_count":
            info = payload.get("info")
            if isinstance(info, dict) and isinstance(info.get("last_token_usage"), dict):
                tot = info.get("total_token_usage")
                if tot is not None and tot == prev_tot:
                    continue
                prev_tot = tot
                lu = info["last_token_usage"]
                inp_tok, cached_tok = int(lu.get("input_tokens") or 0), int(lu.get("cached_input_tokens") or 0)
                usages.append((session_id, cwd, ts, max(inp_tok - cached_tok, 0), cached_tok,
                               int(lu.get("cache_write_input_tokens") or 0), int(lu.get("output_tokens") or 0), f"{session_id}:u:{line_no}"))
            continue
        if ptype == "message":
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            content = payload.get("content")
            blocks = content if isinstance(content, list) else [{"text": content}]
            texts = [b.get("text") or b.get("input_text") or b.get("output_text") for b in blocks if isinstance(b, dict)]
            text = "\n".join(t.strip() for t in texts if isinstance(t, str) and t.strip() and not CODEX_INJECTED.match(t))
            if role == "user":
                text = clean_codex_request(text)
            if text:
                chunks.append(make_chunk(session_id, cwd, ts, role, text, f"{session_id}:{line_no}", current_model))
        elif ptype in ("function_call", "custom_tool_call"):
            cid, name = payload.get("call_id"), payload.get("name", "")
            if any(t in name for t in MEMORY_TOOLS):
                mem_used = True
            raw_args = payload.get("arguments") if "arguments" in payload else payload.get("input", "")
            args = json.dumps(raw_args, ensure_ascii=False) if isinstance(raw_args, dict) else str(raw_args)
            pending_funcs[cid] = {"name": name, "args": trim_tool_output(args.strip()), "ts": ts, "pos": str(line_no), "model": current_model}
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            cid = payload.get("call_id")
            out, call = payload.get("output", ""), pending_funcs.pop(cid, {})
            if out or call:
                chunks.append(tool_chunk(session_id, cwd, call.get("ts", ts), f"{session_id}:{call.get('pos', line_no)}",
                                         call.get("model") or current_model, call.get("name", ""), call.get("args", ""), out,
                                         payload.get("is_error") is True))
    has_turns = bad or any(d.get("type") != "session_meta" for _, d, _ in lines)  # a rollout with only session_meta has no turns yet
    return finish(chunks, usages, mem_used, warnings, non_empty, bad, warn=has_turns)
