import time
from mesa_lore.store import Store


def test_rarity_abstention(tmp_path):
    st = Store(tmp_path / "lore.db")
    # Save a large corpus where some words are common and one word is moderately frequent
    with st.con:
        for i in range(500):
            txt = f"Bu bir test planı ve dosya yapısı örneği {i}." + (" kontrolü" if i < 20 else "")
            st.con.execute(
                "INSERT INTO entries (id, project, text, source_type, state) VALUES (?, 'proj', ?, 'fact', 'active')",
                (f"n_{i}", txt)
            )
            st.con.execute("INSERT INTO entries_fts (id, text_norm) VALUES (?, ?)", (f"n_{i}", txt.replace("ı", "i").lower()))
        st.con.execute(
            "INSERT INTO entries (id, project, text, source_type, state) VALUES ('n_nat', 'proj', 'native veritabanı bağlantı ayarları', 'fact', 'active')"
        )
        st.con.execute("INSERT INTO entries_fts (id, text_norm) VALUES ('n_nat', 'native veritabani baglanti ayarlari')")
        st.con.execute(
            "INSERT INTO entries (id, project, text, source_type, state) VALUES ('n_kol', 'proj', 'kolon şema tanımı', 'fact', 'active')"
        )
        st.con.execute("INSERT INTO entries_fts (id, text_norm) VALUES ('n_kol', 'kolon sema tanimi')")

    # A query with 3 common/moderate words plus 1 unseen word must abstain (weighted share ~0.35 < 0.55)
    r = st.search("Kubernetes test planı dosya yapısı", "proj")
    assert r["status"] == "no_match"
    # Even when one of the matched words is less frequent (20/502 docs), 1 unseen word + 3 corpus words (~0.37 share) must abstain
    r_mod = st.search("Kubernetes test planı kontrolü", "proj")
    assert r_mod["status"] == "no_match"

    # 2-word queries where an unseen word would false-match via 3-char '-s' ('NATS'->'nat') or '-an' ('kolan'->'kol') must abstain
    assert st.search("NATS ayarları", "proj")["status"] == "no_match"
    assert st.search("kolan detayı", "proj")["status"] == "no_match"

    # A query with the actual words should match
    r2 = st.search("test planı", "proj")
    assert r2["status"] == "ok"
    assert len(r2["results"]) > 0

    st.close()


def test_kind_label_searchable(tmp_path):
    st = Store(tmp_path / "lore.db")
    n1 = st.save_note("proj", "Veritabanı seçimi PostgreSQL yerine SQLite.", "decision")
    n2 = st.save_note("proj", "Redis cache yüksek bellek tüketimi.", "failed_attempt")
    n3 = st.save_note("proj", "Kargo iade rotası eklenecek.", "todo")

    # Search by kind label terms
    r_dec = st.search("karar veritabanı", "proj")
    assert r_dec["status"] == "ok"
    assert any(x["id"] == n1 for x in r_dec["results"])

    r_fail = st.search("denendi Redis", "proj")
    assert r_fail["status"] == "ok"
    assert any(x["id"] == n2 for x in r_fail["results"])

    r_todo = st.search("yapılacak kargo iade", "proj")
    assert r_todo["status"] == "ok"
    assert any(x["id"] == n3 for x in r_todo["results"])

    st.close()


def test_search_candidate_limit_perf(tmp_path):
    st = Store(tmp_path / "perf.db")
    with st.con:
        for i in range(2000):
            st.con.execute(
                "INSERT INTO entries (id, project, text, source_type, state, dedupe_key) VALUES (?, 'p', ?, 'user', 'active', ?)",
                (f"c_{i}", f"ortak kelimeler veri akışı test kontrol sonuç {i}", f"d_{i}")
            )
            st.con.execute(
                "INSERT INTO entries_fts (id, text_norm) VALUES (?, ?)",
                (f"c_{i}", f"ortak kelimeler veri akisi test kontrol sonuc {i}")
            )

    t0 = time.perf_counter()
    res = st.search("veri akışı test kontrol", "p")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert res["status"] == "ok"
    assert elapsed_ms < 50  # Should be very fast with LIMIT 200

    st.close()
