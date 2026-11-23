"""R6: search on large DBs. Guards the query shape that made real-DB search p95 ~700 ms -> see PROGRESS (R6)."""
import sqlite3
from mesa_lore.store import SCHEMA_VERSION, Store


def _seed(st):
    for i in range(30):
        st.save_note("app", f"Parser cache rule {i}: tokenizer keeps the first verification token", "decision")
    old = st.save_note("app", "Parser cache uses Redis", "decision")
    st.save_note("app", "Parser cache switched to an in-process LRU", "decision", replaces=old)
    st.con.execute("INSERT INTO handoffs (id, project, text, session_id, ts, state, agent) VALUES "
                   "('h_1', 'app', 'Yapılan: parser cache verification token fixed', 's1', '2026-11-20T10:00:00Z', 'active', 'claude')")
    st.con.execute("INSERT INTO entries_fts (id, text_norm) VALUES ('h_1', 'handoff parser cache verification token fixed')")
    st.con.commit()


def _trace(st):
    seen = []
    st.con.set_trace_callback(seen.append)
    return seen


def test_one_fts_match_per_query_variant(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        _seed(st)
        seen = _trace(st)
        res = st.search(["parser cache token", "verification token first"], "app")
        assert res["status"] == "ok" and any(r["id"] == "h_1" for r in res["results"])
        ranked = [s for s in seen if "MATCH" in s and "count(*)" not in s]
        assert len(ranked) == 2, ranked  # was 3 per variant: non-tool, tool and handoff stages each re-ran the MATCH
    finally:
        st.close()


def test_total_doc_count_does_not_scan_fts_content(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        _seed(st)
        seen = _trace(st)
        st.search("parser cache", "app")
        assert not any(" ".join(s.split()) == "SELECT count(*) FROM entries_fts" for s in seen)  # 14 ms/query on the real DB
        assert st.con.execute("SELECT count(*) FROM entries_fts_docsize").fetchone()[0] == st.con.execute("SELECT count(*) FROM entries_fts").fetchone()[0]
    finally:
        st.close()


def test_replaces_lookup_uses_an_index_and_v7_db_migrates(tmp_path):
    db = tmp_path / "lore.db"
    st = Store(db)
    try:
        _seed(st)
        plan = " ".join(r[3] for r in st.con.execute("EXPLAIN QUERY PLAN SELECT id FROM entries WHERE replaces = ?", ("x",)))
        assert "USING INDEX" in plan, plan  # was a full scan per returned note (18 ms/query on the real DB)
        # a v7 DB (no index yet) opens, migrates and keeps its rows
        st.con.execute("DROP INDEX IF EXISTS idx_entries_replaces")
        st.con.execute("DELETE FROM schema_version WHERE version >= 8")
        st.con.commit()
    finally:
        st.close()
    con = sqlite3.connect(db)
    n = con.execute("SELECT count(*) FROM entries").fetchone()[0]
    con.close()
    st = Store(db)
    try:
        assert st._current_schema_version() == SCHEMA_VERSION >= 8
        assert st.con.execute("SELECT count(*) FROM entries").fetchone()[0] == n
        assert st.con.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_entries_replaces'").fetchone()
    finally:
        st.close()


def test_ranking_filters_scope_before_bm25_without_reading_fts_text(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        _seed(st)
        st.save_note("other", "Parser cache secret of another project", "decision")
        seen = _trace(st)
        res = st.search("parser cache", "app")
        ranked = [s for s in seen if "MATCH" in s and "count(*)" not in s]
        assert ranked and all("lore_in_scope(rowid)" in s and "SELECT id" not in s for s in ranked), ranked
        assert all(r["project"] in ("app", "global") for r in res["results"])
        assert st.con.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_fts_content_id'").fetchone()
    finally:
        st.close()


def test_small_scope_is_not_crowded_out_by_another_projects_hits(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        for i in range(260):
            st.save_note("big", f"needle needle needle {i}", "fact")
        mine = st.save_note("small", "a much longer note where the needle appears once among many other ordinary words here", "fact")
        assert [r["id"] for r in st.search("needle", "small")["results"]] == [mine]
    finally:
        st.close()


def test_search_survives_vacuum_and_new_rows(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        _seed(st)
        before = [r["id"] for r in st.search("parser cache token", "app")["results"]]
        st.con.commit()
        st.con.execute("VACUUM")  # may renumber implicit entries rowids; the FTS link must not depend on them
        assert [r["id"] for r in st.search("parser cache token", "app")["results"]] == before
        new = st.save_note("app", "Zebra parser cache token added after vacuum", "decision")
        assert new in [r["id"] for r in st.search("zebra parser", "app")["results"]]
    finally:
        st.close()


def test_search_stays_correct_without_the_speed_index(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        st.con.execute("DROP INDEX IF EXISTS idx_fts_content_id")
        st.con.commit()
        nid = st.save_note("app", "Parser cache without the speed index", "decision")
        assert [r["id"] for r in st.search("parser cache", "app")["results"]] == [nid]  # slower, still correct
    finally:
        st.close()


def test_each_row_is_normalized_once_across_query_variants(tmp_path, monkeypatch):
    from mesa_lore import store as store_mod
    st = Store(tmp_path / "lore.db")
    try:
        st.save_note("app", "Parser cache token MARKERTEXT verification", "decision")
        calls, orig = [], store_mod.normalize
        monkeypatch.setattr(store_mod, "normalize", lambda s: (calls.append(s) if "MARKERTEXT" in (s or "") else None) or orig(s))
        assert st.search(["parser cache", "cache token", "token verification"], "app")["status"] == "ok"
        assert len(calls) == 1, calls  # was once per query variant
    finally:
        st.close()


def test_scope_lookup_uses_a_covering_index(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        plan = " ".join(r[3] for r in st.con.execute("EXPLAIN QUERY PLAN SELECT e.id, e.source_type, e.kind FROM entries e WHERE e.project IN ('a', 'global')"))
        assert "COVERING INDEX idx_entries_scope" in plan, plan  # reading full rows for source_type cost 8 ms per search
    finally:
        st.close()


def test_ascii_fast_path_matches_turkish_normalization():
    from mesa_lore.text import normalize, tr_lower
    for s in ("MIGRATION", "Fixed I/O in FILE_PATH v0.9.2", "ISTANBUL_ID", "plain", "İSTANBUL Iğdır ÇÖŞ", "mixed I İ ı i"):
        assert normalize(s) == tr_lower(s).replace("ı", "i")


def test_v8_merges_fts_segments_once_and_keeps_rows(tmp_path):
    db = tmp_path / "lore.db"
    st = Store(db)
    try:
        for i in range(20):  # each commit writes its own FTS segment
            st.save_note("app", f"Parser cache segment note number {i}", "fact")
        assert st.con.execute("SELECT count(DISTINCT segid) FROM entries_fts_idx").fetchone()[0] > 1
        st.con.execute("DELETE FROM schema_version WHERE version >= 8")
        st.con.commit()
    finally:
        st.close()
    st = Store(db)
    try:
        assert st.con.execute("SELECT count(DISTINCT segid) FROM entries_fts_idx").fetchone()[0] == 1  # fewer posting lists to merge per MATCH
        assert len(st.search("parser cache segment", "app", budget_tokens=2000)["results"]) == 20
    finally:
        st.close()


def test_row_deleted_between_ranking_and_fetch_is_skipped(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        keep = st.save_note("app", "Parser cache keeps this note", "decision")
        gone = st.save_note("app", "Parser cache loses this note", "decision")
        real = st.con

        class Racing:  # another process purges the note right after ranking
            def execute(self, sql, args=()):
                if "e.id IN (" in sql:
                    real.execute("DELETE FROM entries WHERE id = ?", (gone,))
                return real.execute(sql, args)

            def __getattr__(self, name):
                return getattr(real, name)
        st.con = Racing()
        ids = [r["id"] for r in st.search("parser cache note", "app")["results"]]
        st.con = real
        assert ids == [keep]
    finally:
        st.close()
