from mesa_lore.store import Store


def test_supersession_pair_returned_together(tmp_path):
    db_path = tmp_path / "test.db"
    st = Store(db_path)
    try:
        n1 = st.save_note(
            "proj",
            "Veritabanı olarak PostgreSQL seçildi.",
            "decision",
            agent="claude-code"
        )
        n2 = st.save_note(
            "proj",
            "Veritabanı: MVP için PostgreSQL yerine SQLite kullanılacak (kurulum kolaylığı).",
            "decision",
            replaces=n1,
            agent="claude-code"
        )

        # Query matching the superseded note
        r1 = st.search("Veritabanı olarak ilk başta ne seçildi?", "proj")
        assert r1["status"] == "ok"
        items1 = r1["results"]
        assert len(items1) >= 2
        ids1 = [x["id"] for x in items1[:2]]
        assert n1 in ids1 and n2 in ids1
        states1 = {x["id"]: x["state"] for x in items1[:2]}
        assert states1[n1] == "superseded"
        assert states1[n2] == "active"

        # Query matching the active replacement note
        r2 = st.search("MVP için SQLite kurulum kolaylığı", "proj")
        assert r2["status"] == "ok"
        items2 = r2["results"]
        assert len(items2) >= 2
        ids2 = [x["id"] for x in items2[:2]]
        assert n1 in ids2 and n2 in ids2

        # When n2 is superseded again by n3, searching for n2 must also return the current active note n3
        n3 = st.save_note(
            "proj",
            "Veritabanı: yüksek eşzamanlılık için DuckDB motoruna geçildi.",
            "decision",
            replaces=n2,
            agent="codex"
        )
        r3 = st.search("MVP için SQLite kurulum kolaylığı", "proj")
        ids3 = [x["id"] for x in r3["results"]]
        assert n2 in ids3 and n3 in ids3
    finally:
        st.close()


def test_superseded_pair_budget_constrained(tmp_path):
    db_path = tmp_path / "test.db"
    st = Store(db_path)
    try:
        # n1 is ~11 tokens, n2 is ~11 tokens
        n1 = st.save_note(
            "proj",
            "Oturum için Redis kümesi kararlaştırıldı.",
            "decision"
        )
        n2 = st.save_note(
            "proj",
            "Oturum için yerel SQLite WAL moduna geçildi.",
            "decision",
            replaces=n1
        )
        # Budget 15 tokens: only enough for one note (~11 tokens)
        r = st.search("Oturum Redis", "proj", budget_tokens=15)
        assert r["status"] == "ok"
        assert len(r["results"]) == 1
        assert r["tokens"] <= 15
    finally:
        st.close()
