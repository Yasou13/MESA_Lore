import json
from mesa_lore.store import Store


def test_session_diversity_cap_and_null_notes_preserved(tmp_path):
    tf1 = tmp_path / "sa.jsonl"
    tf1.write_text("\n".join([
        json.dumps({"type": "assistant", "sessionId": "sa", "cwd": str(tmp_path), "timestamp": f"2026-11-10T10:00:0{i}Z",
                    "message": {"role": "assistant", "content": f"kuyruk deadletter ayarı partA_{i}"}})
        for i in range(4)
    ]) + "\n", encoding="utf-8")
    tf2 = tmp_path / "sb.jsonl"
    tf2.write_text(json.dumps({
        "type": "assistant", "sessionId": "sb", "cwd": str(tmp_path), "timestamp": "2026-11-10T09:00:00Z",
        "message": {"role": "assistant", "content": "kuyruk deadletter ayarı partB_0"},
    }) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        proj = tmp_path.name
        st.ingest_file(tf1, "claude")
        st.ingest_file(tf2, "claude")
        res = st.search("kuyruk deadletter ayarı", proj, budget_tokens=800)
        assert res["status"] == "ok"
        sa_hits = [r for r in res["results"] if r["session_id"] == "sa" and r["source_type"] != "note"]
        sb_hits = [r for r in res["results"] if r["session_id"] == "sb" and r["source_type"] != "note"]
        assert len(sa_hits) <= 2
        assert len(sb_hits) == 1

        # session_id=None notes are never collapsed together
        n1 = st.save_note(proj, "cache ttl token policy alpha", "fact")
        n2 = st.save_note(proj, "cache ttl token policy beta", "fact")
        n3 = st.save_note(proj, "cache ttl token policy gamma", "fact")
        nres = st.search("cache ttl token policy", proj, budget_tokens=800, kind="fact")
        returned_ids = {r["id"] for r in nres["results"]}
        assert {n1, n2, n3}.issubset(returned_ids)
    finally:
        st.close()


def test_source_weighting_assistant_over_tool_and_tool_only_ok(tmp_path):
    tf = tmp_path / "s.jsonl"
    tf.write_text("\n".join([
        json.dumps({"type": "assistant", "sessionId": "s1", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:00:00Z",
                    "message": {"role": "assistant", "content": "Use ZSTD_LEVEL_3 for telemetri sıkıştırma algoritması"}}),
        json.dumps({"type": "assistant", "sessionId": "s1", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:00:01Z",
                    "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": "grep telemetri sıkıştırma"}]}}),
        json.dumps({"type": "user", "sessionId": "s1", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:00:02Z",
                    "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "telemetri sıkıştırma algoritması log dump tool output only_tool_marker"}]}}),
    ]) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        proj = tmp_path.name
        st.ingest_file(tf, "claude")
        res = st.search("telemetri sıkıştırma algoritması", proj, budget_tokens=800)
        assert res["status"] == "ok"
        assert res["results"][0]["source_type"] == "assistant"
        assert "ZSTD_LEVEL_3" in res["results"][0]["text"]

        t_only = st.search("only_tool_marker", proj, budget_tokens=800)
        assert t_only["status"] == "ok"
    finally:
        st.close()


def test_config_symbol_tie_breaker_beats_short_chatter(tmp_path):
    tf = tmp_path / "tie.jsonl"
    msgs = [
        "kuyruk ayarı için PART1_DLQ_MAX_RETRY=4 tanımlandı.",
        "kuyruk ayarı metrik açıldı.",
        "kuyruk ayarı log seviyesi info.",
    ]
    tf.write_text("\n".join([
        json.dumps({"type": "assistant", "sessionId": "s1", "cwd": str(tmp_path), "timestamp": f"2026-11-10T10:00:0{i}Z",
                    "message": {"role": "assistant", "content": m}})
        for i, m in enumerate(msgs)
    ]) + "\n", encoding="utf-8")
    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(tf, "claude")
        res = st.search("kuyruk ayarı", tmp_path.name, budget_tokens=800)
        assert res["status"] == "ok"
        assert any("PART1_DLQ_MAX_RETRY=4" in r["text"] for r in res["results"])
    finally:
        st.close()
