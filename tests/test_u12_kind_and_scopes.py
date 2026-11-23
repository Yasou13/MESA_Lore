from mesa_lore.store import Store
from mesa_lore.mcp_server import TOOL_SPECS
import json
import math


def test_kind_filter(tmp_path):
    st = Store(tmp_path / "lore.db")
    st.save_note("proj", "Veritabanı seçimi SQLite", "decision")
    st.save_note("proj", "Redis cache denendi bağlantı hatası", "failed_attempt")
    st.save_note("proj", "Kargo takip formatı KRG-12345", "fact")
    st.save_note("proj", "Kargo iade rotası eklenecek", "todo")

    # Even if 250+ transcript chunks match the query with high BM25, kind filter must still find the note
    with st.con:
        for i in range(250):
            txt = f"SQLite SQLite SQLite veritabanı seçimi SQLite {i}"
            st.con.execute(
                "INSERT INTO entries (id, project, text, source_type, state) VALUES (?, 'proj', ?, 'user', 'active')",
                (f"c_{i}", txt)
            )
            st.con.execute("INSERT INTO entries_fts (id, text_norm) VALUES (?, ?)", (f"c_{i}", txt.lower()))

    # Filter by decision
    r_dec = st.search("SQLite", "proj", kind="decision")
    assert r_dec["status"] == "ok"
    assert len(r_dec["results"]) >= 1
    assert all(x["kind"] == "decision" and x["source_type"] == "note" for x in r_dec["results"])

    # Filter by failed_attempt
    r_fail = st.search("Redis cache", "proj", kind="failed_attempt")
    assert r_fail["status"] == "ok"
    assert all(x["kind"] == "failed_attempt" for x in r_fail["results"])

    # Filter by todo for unrelated query
    r_none = st.search("Redis cache", "proj", kind="todo")
    assert r_none["status"] == "no_match"
    assert r_none["results"] == []

    st.close()


def test_scope_map_in_index(tmp_path):
    st = Store(tmp_path / "lore.db")
    st.save_note("kargo-api", "MVP SQLite kullanımı", "decision")
    st.save_note("tez-notlari", "Tez teslim 15 Ocak", "fact")
    st.save_note("research:memory", "LongMemEval Zep skoru %64", "fact")
    st.save_note("global", "Kısa cevap tercihi", "fact")

    idx = st.index("kargo-api", budget_tokens=400)
    lines = idx.splitlines()
    assert lines[0].startswith("Scopes:")
    assert "kargo-api" in lines[0]
    assert "+1 other projects" in lines[0]
    assert "tez-notlari" not in lines[0]
    assert "+1 research scopes" in lines[0] and "research:memory" not in idx  # C4-8: count only
    assert "global" in lines[0]

    # Must contain project's note content, but NOT other scopes' note content
    assert "SQLite" in idx
    assert "15 Ocak" not in idx
    assert "%64" not in idx

    st.close()


def test_tool_specs_size_with_kind():
    search_spec = next(t for t in TOOL_SPECS if t["name"] == "memory_search")
    assert "kind" in search_spec["inputSchema"]["properties"]
    size = math.ceil(len(json.dumps(TOOL_SPECS, ensure_ascii=False)) / 4)
    assert size <= 500
