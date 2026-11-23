import json
import math
from mesa_lore.store import Store
from mesa_lore.mcp_server import TOOL_SPECS, memory_search


def test_tool_specs_size_and_descriptions():
    names = sorted(t["name"] for t in TOOL_SPECS)
    assert names == ["memory_get", "memory_index", "memory_save", "memory_search"]
    size = math.ceil(len(json.dumps(TOOL_SPECS, ensure_ascii=False)) / 4)
    assert size <= 500

    search_spec = next(t for t in TOOL_SPECS if t["name"] == "memory_search")
    assert "synonyms or English" in search_spec["description"]
    assert "2-5 keywords" in search_spec["description"]


def test_store_search_multi_query(tmp_path):
    db_path = tmp_path / "test.db"
    st = Store(db_path)
    try:
        st.save_note("proj", "Parola sıfırlama servisi SendGrid ile entegre edildi.", "decision")
        st.save_note("proj", "Kullanıcı refresh token geçerlilik süresi 7 gün.", "decision")

        # One query variant misses, the other matches
        r = st.search(["unrelated absent topic", "refresh token süresi"], "proj")
        assert r["status"] == "ok"
        assert any("7 gün" in x["text"] for x in r["results"])

        # Both variants match different items
        r2 = st.search(["SendGrid parola sıfırlama", "refresh token 7 gün"], "proj")
        assert r2["status"] == "ok"
        assert any("SendGrid" in x["text"] for x in r2["results"])
        assert any("7 gün" in x["text"] for x in r2["results"])
    finally:
        st.close()


def test_mcp_memory_search_list_query(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MESA_LORE_DB", str(db_path))
    st = Store(db_path)
    try:
        st.save_note("proj", "OAuth2 PKCE akışı zorunlu kılındı.", "decision")
    finally:
        st.close()

    out = memory_search(query=["PKCE akışı", "OAuth2"], project="proj")
    assert "PKCE" in out
