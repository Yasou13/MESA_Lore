from mesa_lore.store import Store
from mesa_lore.text import STOP_WORDS, TR_STOP_WORDS, normalize


def test_stopwords_extended():
    # Turkish question words and inflections
    for w in [
        "nedir", "neydi", "nelerdir", "nasıl", "hangi", "kaç", "kaçtır",
        "acaba", "mıydı", "miydi", "muydu", "müydü", "nerede", "nereye"
    ]:
        norm = normalize(w)
        assert norm in TR_STOP_WORDS or norm in STOP_WORDS

    # English words
    for w in ["use", "used", "using", "which", "what"]:
        assert normalize(w) in STOP_WORDS


def test_natural_queries_match(tmp_path):
    db_path = tmp_path / "test.db"
    st = Store(db_path)
    try:
        st.save_note("proj", "Parola karma algoritması için Argon2id seçildi, memory_cost 64MB.", "decision", agent="claude")

        # Natural question with filler words
        r = st.search("Acaba parola karma algoritması nedir?", "proj")
        assert r["status"] == "ok"
        assert any("Argon2id" in x["text"] for x in r["results"])

        # English natural query with used
        st.save_note("proj", "We use PostgreSQL 16 database for primary data storage.", "decision", agent="codex")
        r2 = st.search("Which database is used for primary storage?", "proj")
        assert r2["status"] == "ok"
        assert any("PostgreSQL" in x["text"] for x in r2["results"])
    finally:
        st.close()


def test_agent_name_and_saver_indexed(tmp_path):
    db_path = tmp_path / "test.db"
    st = Store(db_path)
    try:
        # Note with agent "codex-mcp-client"
        st.save_note("proj", "Kullanıcı mesaj kuyruğu için Kafka istedi.", "fact", agent="codex-mcp-client")

        # Query mentioning agent name
        r = st.search("codex-mcp-client hangi mesaj kuyruğu bilgisini kaydetti?", "proj")
        assert r["status"] == "ok"
        assert any("Kafka" in x["text"] for x in r["results"])

        # Query for note without note text matching all query words
        r2 = st.search("codex-mcp-client Kafka", "proj")
        assert r2["status"] == "ok"
        assert any("Kafka" in x["text"] for x in r2["results"])
    finally:
        st.close()
