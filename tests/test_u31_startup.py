import json
import math
from mesa_lore.mcp_server import TOOL_SPECS
from mesa_lore.store import Store


def _tok(s: str) -> int:
    return math.ceil(len(s) / 4)


def test_startup_index_plus_tool_specs_under_300_tokens(tmp_path):
    tf = tmp_path / "s.jsonl"
    tf.write_text("\n".join([
        json.dumps({"type": "user", "sessionId": f"s{i}", "cwd": "/repo/app", "timestamp": f"2026-11-0{i+1}T10:00:00Z",
                    "message": {"role": "user", "content": f"Session goal number {i} for application startup workflow with extra descriptive words"}})
        for i in range(5)
    ]) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(tf, "claude")
        for i in range(6):
            st.save_note("app", f"Decision note #{i} detailing architecture and deployment details", "decision" if i == 0 else "fact")
        st.save_note("global", "Global preference concise output", "fact")
        st.save_note("research:eval", "Research isolate item", "fact")
        st.save_note("other-proj", "Must stay hidden", "fact")

        idx = st.index("app", budget_tokens=400)
        tools_json = json.dumps(TOOL_SPECS, ensure_ascii=False)
        total = _tok(idx) + _tok(tools_json)
        assert total <= 450, f"startup too large: idx={_tok(idx)} + tools={_tok(tools_json)} = {total}"
        assert idx.startswith("Scopes:")
        assert "memory_search" in idx and "memory_save" in idx
        assert "other-proj" not in idx and "Must stay hidden" not in idx
        assert "global" in idx and "+1 research scopes" in idx and "research:eval" not in idx  # C4-8: count only
        # Shows last 3 handoffs (s2, s3, s4 -> dates 03, 04, 05), not all 5
        assert "2026-11-05" in idx and "2026-11-04" in idx and "2026-11-03" in idx
        assert "2026-11-01" not in idx
    finally:
        st.close()
