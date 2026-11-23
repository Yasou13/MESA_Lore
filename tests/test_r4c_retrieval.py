import json
import math
import sqlite3
from mesa_lore.store import SCHEMA_VERSION, Store
from mesa_lore.text import fold_tr, normalize


def test_ascii_folding_bidirectional_and_exact_priority(tmp_path):
    assert normalize("yönetimi") == "yönetimi"
    assert fold_tr("ŞĞÜÖÇıİ şğüöç") == "sguocii sguoc"

    st = Store(tmp_path / "lore.db")
    try:
        n_tr = st.save_note("proj", "Kullanıcı oturum yönetimi şifreleme ayarlandı.", "decision")
        n_ascii = st.save_note("proj", "Kullanici oturum yonetimi sifreleme ayarlandi.", "decision")
        n_stem = st.save_note("proj", "Arka plan işçisi sürekli çalışıyor.", "fact")

        # Unaccented query finds both Turkish and ASCII notes, ranking exact ASCII first
        r_ascii = st.search("yonetimi sifreleme", "proj", budget_tokens=800)
        ids_a = [r["id"] for r in r_ascii["results"]]
        assert {n_tr, n_ascii}.issubset(set(ids_a))
        assert ids_a[0] == n_ascii

        # Accented query finds both Turkish and ASCII notes, ranking exact Turkish first
        r_tr = st.search("yönetimi şifreleme", "proj", budget_tokens=800)
        ids_t = [r["id"] for r in r_tr["results"]]
        assert {n_tr, n_ascii}.issubset(set(ids_t))
        assert ids_t[0] == n_tr

        # Inflected Turkish word matched by unaccented stem query
        r_stem = st.search("calisiyor", "proj", budget_tokens=800)
        assert any(r["id"] == n_stem for r in r_stem["results"])
    finally:
        st.close()


def test_migration_v7_folds_existing_fts(tmp_path):
    db = tmp_path / "v6.db"
    st = Store(db)
    n_id = st.save_note("proj", "Özel şifreleme modülü aktifleştirildi.", "fact")
    # Simulate a pre-v7 FTS row containing only non-folded text and schema_version=6
    with st.con:
        st.con.execute("UPDATE entries_fts SET text_norm = ? WHERE id = ?", ("fact özel şifreleme modülü aktifleştirildi.", n_id))
        st.con.execute("DELETE FROM schema_version WHERE version >= 7")
    st.close()

    st2 = Store(db)
    try:
        ver = st2.con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert ver == SCHEMA_VERSION >= 7
        res = st2.search("ozel sifreleme modulu", "proj", budget_tokens=800)
        assert res["status"] == "ok"
        assert any(r["id"] == n_id for r in res["results"])
    finally:
        st2.close()


def test_unified_ranking_and_tool_budget_quota(tmp_path):
    tf = tmp_path / "sess.jsonl"
    lines = []
    # Low-relevance older assistant message (bare partial match on "zeta_probe" only)
    lines.append(json.dumps({
        "type": "assistant", "sessionId": "s_old", "cwd": str(tmp_path), "timestamp": "2026-10-01T10:00:00Z",
        "message": {"role": "assistant", "content": "Mentioned zeta_probe briefly without details."},
    }))
    # Newer exact tool output matching all query terms with rich config details
    lines.append(json.dumps({
        "type": "user", "sessionId": "s_new", "cwd": str(tmp_path), "timestamp": "2026-11-10T10:00:00Z",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0", "content": "zeta_probe exact_token_alpha ok"}]},
    }))
    # Multiple ~100-token tool chunks + assistant chunks sharing quota_marker_xyz
    for i in range(6):
        lines.append(json.dumps({
            "type": "user", "sessionId": f"s_t{i}", "cwd": str(tmp_path), "timestamp": f"2026-11-11T10:00:0{i}Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tq{i}",
                "content": f"quota_marker_xyz only_tool_quota_xyz step_{i} " + ("word " * 70)}]},
        }))
    for i in range(3):
        lines.append(json.dumps({
            "type": "assistant", "sessionId": f"s_a{i}", "cwd": str(tmp_path), "timestamp": f"2026-11-11T11:00:0{i}Z",
            "message": {"role": "assistant", "content": f"quota_marker_xyz decision_{i} " + ("detail " * 60)},
        }))
    tf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(tf, "claude")
        proj = tmp_path.name
        # 1) Unified ranking: full-match newer tool beats bare-partial older assistant
        r1 = st.search("zeta_probe exact_token_alpha", proj, budget_tokens=800)
        assert r1["results"][0]["source_type"] == "tool"
        assert "exact_token_alpha" in r1["results"][0]["text"]

        # 2) Tool quota: mixed results cap tool chunks at <= 40% of budget (320 / 800)
        r2 = st.search("quota_marker_xyz", proj, budget_tokens=800)
        t_toks = sum(math.ceil(len(x["text"]) / 4) for x in r2["results"] if x["source_type"] == "tool")
        assert t_toks <= 320
        assert any(x["source_type"] == "assistant" for x in r2["results"])

        # 3) Tool quota even when only ~100-token tool chunks match (<= 320 / 800)
        r3 = st.search("only_tool_quota_xyz", proj, budget_tokens=800)
        t_toks3 = sum(math.ceil(len(x["text"]) / 4) for x in r3["results"] if x["source_type"] == "tool")
        assert 0 < t_toks3 <= 320
    finally:
        st.close()


def test_tool_chunk_auto_attaches_prev_assistant_within_budget(tmp_path):
    tf = tmp_path / "nb.jsonl"
    long_asst = "Switching cache TTL to 45 seconds before running probe. " + ("context " * 80)
    lines = [
        {"type": "assistant", "sessionId": "s_other", "cwd": str(tmp_path), "timestamp": "2026-11-12T09:00:00Z",
         "message": {"role": "assistant", "content": "Unrelated other session assistant message."}},
        {"type": "assistant", "sessionId": "s_nb", "cwd": str(tmp_path), "timestamp": "2026-11-12T10:00:00Z",
         "message": {"role": "assistant", "content": long_asst}},
        {"type": "user", "sessionId": "s_nb", "cwd": str(tmp_path), "timestamp": "2026-11-12T10:00:05Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_nb",
             "content": "cache_verify_marker exit_code=0 " + ("out " * 30)}]}},
    ]
    tf.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(tf, "claude")
        proj = tmp_path.name
        raw_tool = next(e for e in st.list_entries(proj) if e["source_type"] == "tool")
        raw_toks = math.ceil(len(raw_tool["text"]) / 4)

        r = st.search("cache_verify_marker", proj, budget_tokens=800)
        assert r["status"] == "ok"
        hit = next(x for x in r["results"] if x["id"] == raw_tool["id"])
        assert "Switching cache TTL to 45 seconds" in hit["text"]
        assert "Unrelated other session" not in hit["text"]
        # Attached portion <= 60 tokens
        added_chars = len(hit["text"]) - len(raw_tool["text"])
        assert 0 < math.ceil(added_chars / 4) <= 60
        assert r["tokens"] == sum(math.ceil(len(x["text"]) / 4) for x in r["results"]) <= 800

        # Tight budget just above raw tool chunk must still stay <= budget_tokens
        tight_budget = raw_toks + 20
        r_tight = st.search("cache_verify_marker", proj, budget_tokens=tight_budget)
        assert r_tight["status"] == "ok"
        assert r_tight["tokens"] == sum(math.ceil(len(x["text"]) / 4) for x in r_tight["results"]) <= tight_budget
    finally:
        st.close()
