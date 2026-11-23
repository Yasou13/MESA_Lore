import json, re
from pathlib import Path
from mesa_lore.readers import MEMORY_TOOLS, collect_warning, has_session_start_ctx, make_chunk, make_usage, read_jsonl
from mesa_lore.text import trim_tool_output

# Context Codex adds as "user" messages: AGENTS.md, environment and IDE state.
CODEX_INJECTED = re.compile(r"\s*(?:# AGENTS\.md instructions|<environment_context>|<user_instructions>|<external_)")


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
        if ltype == "session_meta":
            if isinstance(payload.get("source"), dict) and "subagent" in payload["source"]:
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
            stype, content, texts = ("user" if role == "user" else "assistant"), payload.get("content"), []
            if isinstance(content, str) and content.strip():
                texts.append(content.strip())
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict):
                        t = blk.get("text") or blk.get("input_text") or blk.get("output_text")
                        if t and isinstance(t, str) and t.strip() and not CODEX_INJECTED.match(t):
                            texts.append(t.strip())
            if texts:
                chunks.append(make_chunk(session_id, cwd, ts, stype, "\n".join(texts), f"{session_id}:{line_no}", current_model))
        elif ptype in ("function_call", "custom_tool_call"):
            cid, name = payload.get("call_id"), payload.get("name", "")
            if any(t in name for t in MEMORY_TOOLS):
                mem_used = True
            raw_args = payload.get("arguments") if "arguments" in payload else payload.get("input", "")
            args = json.dumps(raw_args, ensure_ascii=False) if isinstance(raw_args, dict) else str(raw_args)
            pending_funcs[cid] = {"name": name, "args": trim_tool_output(args.strip()), "ts": ts, "pos": str(line_no), "model": current_model}
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            cid = payload.get("call_id")
            out = payload.get("output", "")
            out = trim_tool_output((json.dumps(out, ensure_ascii=False) if isinstance(out, (dict, list)) else str(out)).strip(), True)
            if pcall := pending_funcs.pop(cid, None):
                merged = f"{pcall['name']} {pcall['args']}\n{out}".strip()
                chunks.append(make_chunk(session_id, cwd, pcall["ts"], "tool", merged, f"{session_id}:{pcall['pos']}", pcall.get("model") or current_model))
            elif out:
                chunks.append(make_chunk(session_id, cwd, ts, "tool", out, f"{session_id}:{line_no}", current_model))
    out_usages = [make_usage(s, cw, t, ui, cr, cb, out, int(mem_used), dk) for s, cw, t, ui, cr, cb, out, dk in usages]
    if bad or any(d.get("type") != "session_meta" for _, d, _ in lines):  # a rollout with only session_meta has no turns yet
        collect_warning(warnings, non_empty, bad, chunks, out_usages)
    return chunks, out_usages
