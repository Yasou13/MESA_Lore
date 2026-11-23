from mesa_lore.store import Store
from mesa_lore.mcp_server import memory_search


def test_search_capped_at_five_items(tmp_path):
    db_path = tmp_path / "test.db"
    st = Store(db_path)
    try:
        # Save 10 notes matching the query; R4b fills budget instead of stopping at 5
        for i in range(10):
            st.save_note("proj", f"Important architecture rule {i}: always test code.", "decision")

        r = st.search("architecture rule test", "proj", budget_tokens=2000)
        assert r["status"] == "ok"
        assert len(r["results"]) == 10 and r["tokens"] <= 2000

        # Partner notes are still included when budget allows
        st2 = Store(tmp_path / "test_cap_partner.db")
        try:
            for i in range(4):
                st2.save_note("proj", f"Top note {i} about authentication token.", "fact")
            old_n = st2.save_note("proj", "Old note 4 about authentication.", "decision")
            st2.save_note("proj", "New note 4 about authentication token.", "decision", replaces=old_n)
            r2 = st2.search("authentication token", "proj", budget_tokens=2000)
            assert len(r2["results"]) == 6 and any(x["id"] == old_n for x in r2["results"])
        finally:
            st2.close()
    finally:
        st.close()


def test_score_cutoff_keeps_needed_second_result(tmp_path):
    st = Store(tmp_path / "test_second.db")
    try:
        for i in range(100):
            st.save_note("proj", f"genel proje kuralı numara {i} sistem kontrol test", "fact")
        st.save_note("proj", "Argon2id parola algoritması seçildi, Argon2id parola standart.", "decision")
        st.save_note("proj", "Argon2id memory_cost=64MB time_cost=3 olarak ayarlandı.", "fact")

        r = st.search("Argon2id sistem", "proj")
        assert r["status"] == "ok"
        texts = [x["text"] for x in r["results"]]
        assert any("parola algoritması" in t for t in texts)
        assert any("memory_cost=64MB" in t for t in texts)
    finally:
        st.close()


def test_index_scopes_no_leak(tmp_path):
    db_path = tmp_path / "test.db"
    st = Store(db_path)
    try:
        st.save_note("my-proj", "Rule in my proj.", "fact")
        st.save_note("global", "Global rule.", "fact")
        st.save_note("research:agents", "Research note.", "fact")
        st.save_note("secret-proj-1", "Secret 1.", "fact")
        st.save_note("secret-proj-2", "Secret 2.", "fact")

        idx = st.index("my-proj")
        assert "my-proj" in idx
        assert "global" in idx
        assert "+1 research scopes" in idx and "research:agents" not in idx  # C4-8: count only
        assert "+2 other projects" in idx
        assert "secret-proj-1" not in idx
        assert "secret-proj-2" not in idx
    finally:
        st.close()


def test_mcp_memory_search_includes_kind_and_state(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MESA_LORE_DB", str(db_path))
    st = Store(db_path)
    try:
        n1 = st.save_note("proj", "Old database PostgreSQL.", "decision", agent="claude")
        st.save_note("proj", "New database SQLite.", "decision", replaces=n1, agent="claude")
    finally:
        st.close()

    out = memory_search(query="database", project="proj")
    assert "note·decision·active" in out
    assert "note·decision·superseded" in out
