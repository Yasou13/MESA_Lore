import json
from pathlib import Path
from mesa_lore.readers import MEMORY_TOOLS, collect_warning, has_session_start_ctx, make_chunk, make_usage, read_jsonl, tool_chunk
from mesa_lore.text import trim_tool_output


def parse_claude(path, warnings=None):
    session_id = fkey = Path(path).stem  # fkey: dedupe prefix; subagent files reuse the parent sessionId
    cwd, current_model, pending_tools, chunks, usages, seen_msg_ids, mem_used = "", None, {}, [], [], set(), False
    lines, non_empty, bad = read_jsonl(path)
    for line_no, d, raw in lines:
        session_id = d.get("sessionId", session_id)
        cwd = d.get("cwd", cwd)
        if has_session_start_ctx(raw):
            mem_used = True
        ts, mtype, msg = d.get("timestamp"), d.get("type"), d.get("message")
        if not isinstance(msg, dict) or d.get("isMeta"):
            continue
        current_model = msg.get("model") or current_model
        content = msg.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        blocks = blocks if isinstance(blocks, list) else []
        if mtype == "user":
            for sub_i, blk in enumerate(blocks):
                if not isinstance(blk, dict):
                    continue
                btype = blk.get("type")
                if btype == "text" and blk.get("text", "").strip():
                    chunks.append(make_chunk(session_id, cwd, ts, "user", blk["text"].strip(), f"{fkey}:{line_no}" if isinstance(content, str) else f"{fkey}:{line_no}:{sub_i}", current_model))
                elif btype == "tool_result":
                    tid, c = blk.get("tool_use_id"), blk.get("content", "")
                    if isinstance(c, list):
                        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and "text" in b)
                    tcall = pending_tools.pop(tid, {})
                    if c or tcall:
                        chunks.append(tool_chunk(session_id, cwd, tcall.get("ts", ts), f"{fkey}:{tcall.get('pos', f'{line_no}:{sub_i}')}",
                                                 tcall.get("model") or current_model, tcall.get("name", ""), tcall.get("inp", ""),
                                                 c, blk.get("is_error") is True))
        elif mtype == "assistant":
            if isinstance(u := msg.get("usage"), dict) and (mid := msg.get("id") or f"line_{line_no}") not in seen_msg_ids:
                seen_msg_ids.add(mid)
                usages.append((session_id, cwd, ts, int(u.get("input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0),
                               int(u.get("cache_creation_input_tokens") or 0), int(u.get("output_tokens") or 0), f"{fkey}:u:{mid}"))
            for sub_i, blk in enumerate(blocks):
                if not isinstance(blk, dict):
                    continue
                btype = blk.get("type")
                if btype == "text" and blk.get("text", "").strip():
                    chunks.append(make_chunk(session_id, cwd, ts, "assistant", blk["text"].strip(), f"{fkey}:{line_no}" if isinstance(content, str) else f"{fkey}:{line_no}:{sub_i}", current_model))
                elif btype == "tool_use":
                    tid, name, inp = blk.get("id"), blk.get("name", ""), blk.get("input", "")
                    if any(t in name for t in MEMORY_TOOLS):
                        mem_used = True
                    inp = json.dumps(inp, ensure_ascii=False) if isinstance(inp, dict) else str(inp)
                    pending_tools[tid] = {"name": name, "inp": trim_tool_output(inp.strip()), "ts": ts, "pos": f"{line_no}:{sub_i}", "model": current_model}
    if current_model:
        first_model = next((c["model"] for c in chunks if c["model"]), current_model)
        for c in chunks:
            c["model"] = c["model"] or first_model
    out_usages = [make_usage(s, cw, t, ui, cr, cb, out, int(mem_used), dk) for s, cw, t, ui, cr, cb, out, dk in usages]
    collect_warning(warnings, non_empty, bad, chunks, out_usages)
    return chunks, out_usages
