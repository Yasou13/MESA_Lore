import math
import tempfile
from pathlib import Path
from mesa_lore.store import Store


def test_search_isolation_and_global(tmp_path):
    db = tmp_path / "lore.db"
    st = Store(db)

    # Save note in projA, projB, and global
    st.save_note("projA", "ProjA specific database architecture.", "decision")
    st.save_note("projB", "ProjB specific database architecture.", "decision")
    st.save_note("global", "Global rule: use databases wisely.", "fact")

    # Search in projA
    rA = st.search("database architecture", "projA")
    assert rA["status"] == "ok"
    projects = {res["project"] for res in rA["results"]}
    assert "projA" in projects
    assert "projB" not in projects

    # Search in projB
    rB = st.search("database architecture", "projB")
    assert rB["status"] == "ok"
    projects = {res["project"] for res in rB["results"]}
    assert "projB" in projects
    assert "projA" not in projects

    st.close()


def test_search_supersession_ranking(tmp_path):
    db = tmp_path / "lore.db"
    st = Store(db)

    old_id = st.save_note("proj", "Veritabanı seçimi: PostgreSQL seçildi.", "decision")
    new_id = st.save_note("proj", "Veritabanı seçimi: SQLite kullanılacak.", "decision", replaces=old_id)

    res = st.search("veritabanı seçimi", "proj")
    assert res["status"] == "ok"
    assert len(res["results"]) >= 2
    assert res["results"][0]["id"] == new_id
    assert res["results"][0]["state"] == "active"
    assert "SQLite" in res["results"][0]["text"]

    st.close()


def test_search_abstain(tmp_path):
    db = tmp_path / "lore.db"
    st = Store(db)

    st.save_note("proj", "Python backend with FastAPI.", "decision")

    # Completely unrelated query
    res = st.search("Kubernetes cluster setup with Helm", "proj")
    assert res["status"] == "no_match"
    assert res["results"] == []
    assert res["tokens"] == 0

    st.close()


def test_search_budget_trimming(tmp_path):
    db = tmp_path / "lore.db"
    st = Store(db)

    for i in range(10):
        st.save_note("proj", f"Logging configuration item number {i} with detailed information.", "fact")

    # Very small budget (e.g. 25 tokens)
    budget = 25
    res = st.search("Logging configuration", "proj", budget_tokens=budget)
    assert res["status"] == "ok"
    total_tokens = sum(math.ceil(len(r["text"]) / 4) for r in res["results"])
    assert total_tokens <= budget
    assert res["tokens"] == total_tokens

    st.close()


def test_search_turkish_case_and_suffixes(tmp_path):
    db = tmp_path / "lore.db"
    st = Store(db)

    st.save_note("proj", "Kullanıcı istek sınırıyla ilgili karar.", "decision")

    # Query with uppercase and different suffix
    res = st.search("SINIRLAR", "proj")
    assert res["status"] == "ok"
    assert any("sınırıyla" in r["text"].lower() for r in res["results"])

    st.close()


def test_search_fts_syntax_and_edge_queries(tmp_path):
    db = tmp_path / "lore.db"
    st = Store(db)
    st.save_note("kargo-api", "Veritabanı olarak SQLite kullanılacak, dakikada 60 istek sınırı ve İÜ testi.", "decision")
    st.save_note("kargo-api", "MongoDB yerine SQLite seçildi, ne için ve nasıl olduğu not edildi.", "fact")

    # Empty, whitespace, lone punctuation
    for q in ["", "   ", "???", "!@#$%^&*()", '""', "()", "---", ":::", "***"]:
        r = st.search(q, "kargo-api")
        assert r["status"] == "no_match" and r["results"] == []

    # FTS syntax characters and boolean operators must not crash
    for q in [
        '"SQLite', 'SQLite*', '-SQLite', 'project:SQLite', '(SQLite OR Postgres)',
        'SQLite AND dakikada', 'SQLite NOT MongoDB', 'NEAR(SQLite, 60)',
        'AND', 'OR', 'NOT', 'NEAR', 'AND OR NOT NEAR'
    ]:
        r = st.search(q, "kargo-api")
        assert r["status"] in ("ok", "no_match")

    # Only stopwords (TR and EN) must return no_match even if those stopwords appear in stored text
    for q in ["ne için ve nasıl", "mi mı mu mü", "what is the and or", "AND OR NOT", "hangisine"]:
        r = st.search(q, "kargo-api")
        assert r["status"] == "no_match", f"Expected no_match for stopword query {q!r}"

    # 1-2 character words: exact token match works (including Turkish uppercase), substring inside word does not
    assert st.search("60", "kargo-api")["status"] == "ok"
    assert st.search("iü", "kargo-api")["status"] == "ok"
    assert st.search("go", "kargo-api")["status"] == "no_match"  # must not match inside 'kargo' or 'MongoDB'

    # Budget smaller than one result
    r_tiny = st.search("SQLite", "kargo-api", budget_tokens=2)
    assert r_tiny["status"] == "no_match"
    assert r_tiny["results"] == []
    assert r_tiny["tokens"] == 0

    st.close()


def test_search_paraphrased_beyond_dev(tmp_path):
    import json
    bench_dir = Path(__file__).resolve().parent.parent / "bench"
    fix_dir = bench_dir / "fixtures"
    st = Store(tmp_path / "lore.db")
    for p in sorted((fix_dir / "claude").glob("*.jsonl")):
        st.ingest_file(p, agent="claude")
    for p in sorted((fix_dir / "codex").glob("*.jsonl")):
        st.ingest_file(p, agent="codex")
    ids = {}
    for line in (bench_dir / "notes.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            n = json.loads(line)
            ids[n["key"]] = st.save_note(
                n["project"], n["text"], n["kind"], session_id=n["session_id"],
                quote=n["quote"], replaces=ids.get(n.get("replaces")) if n.get("replaces") else None
            )

    paraphrases = [
        ("p01", "kargo-api", "access token kaç dakika geçerli", "ok", None, ["15 dakika"]),
        ("p02", "kargo-api", "What is the refresh token expiration duration?", "ok", None, ["7 gün"]),
        ("p03", "kargo-api", "requests.post hatasını nasıl düzelttik", "ok", None, ["timeout=10"]),
        ("p04", "kargo-api", "Why did the login test fail with TimeoutError?", "ok", None, ["requests.post"]),
        ("p05", "kargo-api", "MVP veritabanı olarak ne kullanıyoruz", "ok", ["SQLite"], None),
        ("p06", "kargo-api", "kullanıcı başına rate limit kaç istek", "ok", None, ["60"]),
        ("p07", "kargo-api", "takip numarası doğrulaması hangi dosyada", "ok", None, ["shipping/tracking.py"]),
        ("p08", "kargo-api", "How many pytest tests passed?", "ok", None, ["12 passed"]),
        ("p09", "tez-notlari", "tez verileri nerede saklanıyor", "ok", None, ["MongoDB"]),
        ("p10", "tez-notlari", "literatür bölümü kaç alt başlık olacak", "ok", None, ["3 alt başlığa"]),
        ("p11", "tez-notlari", "tezin teslim günü ne zaman", "ok", None, ["15 Ocak"]),
        ("p12", "kargo-api", "kullanıcı uzun açıklama ister mi", "ok", None, ["kısa cevap"]),
        ("p13", "kargo-api", "Docker container port numarası kaç", "no_match", None, None),
        ("p14", "kargo-api", "What is the GraphQL subscription endpoint?", "no_match", None, None),
        ("p15", "tez-notlari", "anket katılımcı sayısı kaç kişi", "no_match", None, None),
        ("p16", "tez-notlari", "Where is the rate limit middleware file?", "no_match", None, None),
    ]

    passed = 0
    failed = []
    for qid, proj, query, expect, top1, any_c in paraphrases:
        r = st.search(query, proj, budget_tokens=300)
        items = r["results"]
        texts = [i["text"].replace("I", "ı").replace("İ", "i").lower() for i in items]
        if expect == "no_match":
            ok = r["status"] == "no_match" and not items
        else:
            ok = r["status"] == "ok" and bool(texts)
            if top1:
                ok = ok and all(s.replace("I", "ı").replace("İ", "i").lower() in texts[0] for s in top1)
            if any_c:
                ok = ok and all(any(s.replace("I", "ı").replace("İ", "i").lower() in t for t in texts) for s in any_c)
        if ok:
            passed += 1
        else:
            failed.append((qid, query, r["status"], len(items)))

    st.close()
    assert not failed, f"Paraphrases failed ({passed}/{len(paraphrases)} passed): {failed}"

