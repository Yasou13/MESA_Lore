import json
from pathlib import Path
from mesa_lore.readers import MEMORY_TOOLS, collect_warning, has_session_start_ctx, make_chunk, make_usage, read_jsonl
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
        if mtype == "user":
            if isinstance(content, str) and content.strip():
                chunks.append(make_chunk(session_id, cwd, ts, "user", content.strip(), f"{fkey}:{line_no}", current_model))
            elif isinstance(content, list):
                for sub_i, blk in enumerate(content):
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get("type")
                    if btype == "text" and blk.get("text", "").strip():
                        chunks.append(make_chunk(session_id, cwd, ts, "user", blk["text"].strip(), f"{fkey}:{line_no}:{sub_i}", current_model))
                    elif btype == "tool_result":
                        tid, c = blk.get("tool_use_id"), blk.get("content", "")
                        if isinstance(c, list):
                            c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and "text" in b)
                        c = trim_tool_output((c if isinstance(c, str) else str(c)).strip(), True)
                        if tcall := pending_tools.pop(tid, None):
                            merged = f"{tcall['name']} {tcall['inp']}\n{c}".strip()
                            chunks.append(make_chunk(session_id, cwd, tcall["ts"], "tool", merged, f"{fkey}:{tcall['pos']}", tcall.get("model") or current_model))
                        elif c:
                            chunks.append(make_chunk(session_id, cwd, ts, "tool", c, f"{fkey}:{line_no}:{sub_i}", current_model))
        elif mtype == "assistant":
            if isinstance(u := msg.get("usage"), dict) and (mid := msg.get("id") or f"line_{line_no}") not in seen_msg_ids:
                seen_msg_ids.add(mid)
                usages.append((session_id, cwd, ts, int(u.get("input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0),
                               int(u.get("cache_creation_input_tokens") or 0), int(u.get("output_tokens") or 0), f"{fkey}:u:{mid}"))
            if isinstance(content, str) and content.strip():
                chunks.append(make_chunk(session_id, cwd, ts, "assistant", content.strip(), f"{fkey}:{line_no}", current_model))
            elif isinstance(content, list):
                for sub_i, blk in enumerate(content):
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get("type")
                    if btype == "text" and blk.get("text", "").strip():
                        chunks.append(make_chunk(session_id, cwd, ts, "assistant", blk["text"].strip(), f"{fkey}:{line_no}:{sub_i}", current_model))
                    elif btype == "tool_use":
                        tid, name, inp = blk.get("id"), blk.get("name", ""), blk.get("input", "")
                        if any(t in name for t in MEMORY_TOOLS):
                            mem_used = True
                        inp = json.dumps(inp, ensure_ascii=False) if isinstance(inp, dict) else str(inp)
                        pending_tools[tid] = {"name": name, "inp": trim_tool_output(inp.strip()), "ts": ts, "pos": f"{line_no}:{sub_i}", "model": current_model}
    if current_model:
        first_model = next((c["model"] for c in chunks if c["model"]), current_model)
        for c in chunks:
            if not c["model"]:
                c["model"] = first_model
    out_usages = [make_usage(s, cw, t, ui, cr, cb, out, int(mem_used), dk) for s, cw, t, ui, cr, cb, out, dk in usages]
    collect_warning(warnings, non_empty, bad, chunks, out_usages)
    return chunks, out_usages
