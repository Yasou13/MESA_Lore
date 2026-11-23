"""Locked benchmark for mesa-lore. Do not edit.

Usage:
  python bench/run.py                       # report only
  python bench/run.py --require search,abstain
  python bench/run.py --require all
  python bench/run.py --questions PATH      # other question file (same format)
"""

import argparse
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
SECRETS = ["sk-ant-api03-FAKEKEY1234567890abcdefXYZ", "ghp_FAKEtoken00000000000000000000000000AB"]
TOOL_INPUT_MID = "MIDDLE_ONLY_MARKER_99182"
BUDGET = 300
INDEX_BUDGET = 400
TOOLS_BUDGET = 500
STARTUP_BUDGET = 450
HANDOFF_BUDGET = 120
LEAN_MAX_TOKENS = 180
THRESH = {
    "search": 0.8, "turkish": 0.8, "supersession": 1.0, "abstain": 0.9, "isolation": 1.0,
    "longchunk": 1.0, "kind": 1.0, "scopes": 1.0, "natural": 0.8, "crossagent": 1.0,
    "multiquery": 1.0, "softening": 0.8, "antigravity": 1.0, "toolinput": 1.0,
    "spread": 0.8,
}
PERF_P95_MS = 100
ALL = [
    "ingest", "dedupe", "secrets", "notes", "search", "turkish", "supersession",
    "abstain", "isolation", "budget", "index", "tools",
    "longchunk", "kind", "scopes", "signature", "export", "perf",
    "natural", "crossagent", "multiquery", "softening", "antigravity", "toolinput", "lean",
    "handoff", "neighbors", "spread", "startup", "migrate", "doctor",
]


def tok(s):
    return math.ceil(len(s) / 4)


def low(s):
    return s.replace("İ", "i").replace("I", "ı").lower().replace("ı", "i")


def load(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def checkout(root, name):
    path = root / name
    if not path.exists():
        subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def fixture_checkout(root, path):
    text = path.read_text(encoding="utf-8")
    def mapped(match):
        return checkout(root, match[1]).as_posix()
    text = re.sub(r"/home/dev/projects/([\w.-]+)", mapped, text)
    # Keep each fixture's filename and Antigravity's session directory layout.
    target = root / path.relative_to(FIX)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def run(questions_path):
    res = {}
    try:
        from mesa_lore.store import Store
    except Exception as e:  # noqa: BLE001
        print(f"SKIP all: cannot import mesa_lore.store.Store ({e})")
        return {k: ("SKIP", str(e)) for k in ALL}

    tmp = Path(tempfile.mkdtemp())
    db = tmp / "lore.db"
    st = Store(db)
    files = [(p, "claude") for p in sorted((FIX / "claude").glob("*.jsonl"))]
    files += [(p, "codex") for p in sorted((FIX / "codex").glob("*.jsonl"))]
    files += [(p, "antigravity") for p in sorted((FIX / "antigravity").glob("*/.system_generated/logs/transcript.jsonl"))]

    checkouts = Path(tempfile.mkdtemp())
    files = [(fixture_checkout(checkouts, p), agent) for p, agent in files]
    try:
        first = sum(st.ingest_file(p, agent=a) for p, a in files)
        res["ingest"] = ("PASS" if first > 0 else "FAIL", f"chunks={first}")
    except Exception as e:  # noqa: BLE001
        res["ingest"] = ("FAIL", repr(e))
        return res
    try:
        second = sum(st.ingest_file(p, agent=a) for p, a in files)
        res["dedupe"] = ("PASS" if second == 0 else "FAIL", f"second_ingest={second}")
    except Exception as e:  # noqa: BLE001
        res["dedupe"] = ("FAIL", repr(e))

    ids = {}
    try:
        for n in load(HERE / "notes.jsonl"):
            ids[n["key"]] = st.save_note(
                n["project"], n["text"], n["kind"], session_id=n["session_id"],
                quote=n["quote"], replaces=ids.get(n.get("replaces")) if n.get("replaces") else None,
                agent=n.get("agent"))
        v1, v3, v4 = (st.get(ids[k]) for k in ("n1", "n3", "n4"))
        ok = v1["verified"] is True and v3["verified"] is True and v4["verified"] is False
        ok = ok and v1["state"] == "superseded" and st.get(ids["n2"])["state"] == "active"
        res["notes"] = ("PASS" if ok else "FAIL",
                        f"n1={v1['verified']},{v1['state']} n3={v3['verified']} n4={v4['verified']}")
    except Exception as e:  # noqa: BLE001
        res["notes"] = ("FAIL", repr(e))

    st.close()
    raw = b"".join(p.read_bytes() for p in tmp.iterdir())
    leaked = [s[:12] for s in SECRETS if s.encode() in raw]
    mid_in_db = TOOL_INPUT_MID.encode() in raw
    res["secrets"] = ("FAIL" if leaked else "PASS", f"leaked={leaked}")
    st = Store(db)

    cats, fails, over, leaks, lean_toks = {}, [], 0, 0, []
    questions = load(questions_path)
    for q in questions:
        q_arg = q["queries"] if "queries" in q else q["query"]
        try:
            r = st.search(q_arg, q["project"], budget_tokens=BUDGET, kind=q.get("kind"))
        except Exception as e:  # noqa: BLE001
            r = {"status": "error", "results": [], "err": repr(e)}
        items = r.get("results", [])
        texts = [low(i.get("text", "")) for i in items]
        q_toks = sum(tok(i.get("text", "")) for i in items)
        if q_toks > BUDGET:
            over += 1
        if q["category"] in ("search", "natural") and r.get("status") == "ok" and items:
            lean_toks.append(q_toks)
        leaks += sum(1 for i in items if i.get("project") not in (q["project"], "global"))
        wrong_kind = q.get("kind") and any(i.get("kind") != q["kind"] for i in items)
        if q["expect"] == "no_match":
            good = r.get("status") == "no_match" and not items
        else:
            good = r.get("status") == "ok" and not wrong_kind
            if "top1_contains" in q:
                good = good and bool(texts) and all(low(s) in texts[0] for s in q["top1_contains"])
            if "any_contains" in q:
                good = good and all(any(low(s) in t for t in texts) for s in q["any_contains"])
        c = cats.setdefault(q["category"], [0, 0])
        c[0] += good
        c[1] += 1
        if not good:
            fails.append(f"{q['id']} [{q['category']}] '{q_arg}' -> {r.get('status')}, "
                         f"{len(items)} results")
    for k, (g, n) in cats.items():
        need = THRESH[k]
        ok = g / n >= need and (k != "isolation" or leaks == 0) and (k != "toolinput" or not mid_in_db)
        extra = f" leaks={leaks}" if k == "isolation" else (f" mid_in_db={mid_in_db}" if k == "toolinput" else "")
        res[k] = ("PASS" if ok else "FAIL", f"{g}/{n} (need {need:.0%}){extra}")
    if "toolinput" not in res:
        r_ti = st.search("shipping/tariffs.py desi_carpani", "kargo-api", budget_tokens=BUDGET)
        ti_ok = any("desi_carpani=3000" in i.get("text", "") for i in r_ti.get("results", []))
        res["toolinput"] = ("PASS" if ti_ok and not mid_in_db else "FAIL",
                            f"{int(ti_ok)}/1 (need 100%) mid_in_db={mid_in_db}")
    avg_lean = sum(lean_toks) / len(lean_toks) if lean_toks else 999.0
    res["lean"] = ("PASS" if lean_toks and avg_lean <= LEAN_MAX_TOKENS else "FAIL",
                   f"avg_tokens={avg_lean:.0f} (need <= {LEAN_MAX_TOKENS})")
    res["budget"] = ("PASS" if over == 0 else "FAIL", f"queries_over_budget={over}")

    idx = ""
    try:
        idx = st.index("kargo-api", budget_tokens=INDEX_BUDGET)
        ok = tok(idx) <= INDEX_BUDGET and "SQLite" in idx and "MongoDB" not in idx and "%64" not in idx
        ok = ok and "global" in idx and "+1 research scopes" in idx and "research:" not in idx and "tez-notlari" not in idx
        res["index"] = ("PASS" if ok else "FAIL", f"tokens={tok(idx)}")
    except Exception as e:  # noqa: BLE001
        res["index"] = ("FAIL", repr(e))

    tools_tok = 999
    try:
        from mesa_lore.mcp_server import TOOL_SPECS
        names = sorted(t["name"] for t in TOOL_SPECS)
        tools_tok = tok(json.dumps(TOOL_SPECS, ensure_ascii=False))
        ok = names == ["memory_get", "memory_index", "memory_save", "memory_search"] and tools_tok <= TOOLS_BUDGET
        res["tools"] = ("PASS" if ok else "FAIL", f"tokens={tools_tok} names={names}")
    except Exception as e:  # noqa: BLE001
        res["tools"] = ("SKIP", repr(e))

    try:
        idx_tok = tok(idx)
        has_nudge = "memory_search" in idx and "memory_save" in idx
        ok = bool(idx) and has_nudge and (idx_tok + tools_tok <= STARTUP_BUDGET)
        res["startup"] = ("PASS" if ok else "FAIL",
                          f"index={idx_tok} tools={tools_tok} total={idx_tok + tools_tok} (need <= {STARTUP_BUDGET})")
    except Exception as e:  # noqa: BLE001
        res["startup"] = ("FAIL", repr(e))

    try:
        handoffs = [e for e in st.list_entries()
                    if e.get("source_type") == "note" and e.get("kind") == "handoff" and e.get("state") == "active"]
        by_sess = {e.get("session_id"): e for e in handoffs if e.get("session_id")}
        checks = {"a1": ("PostgreSQL",), "a2": ("SQLite",), "c1": ("KRG-",), "ag1": ("CN23-TR",), "r4n1": ("TLS_CIPHER",)}
        ok_sess = all(
            sid in by_sess and tok(by_sess[sid]["text"]) <= HANDOFF_BUDGET
            and all(low(req) in low(by_sess[sid]["text"]) for req in reqs)
            for sid, reqs in checks.items()
        )
        r_ho = st.search("KRG- takip numarası", "kargo-api", budget_tokens=BUDGET, kind="handoff")
        ho_search = r_ho.get("status") == "ok" and any("KRG-" in x.get("text", "") for x in r_ho.get("results", []))
        max_ho_tok = max((tok(h["text"]) for h in handoffs), default=0)
        res["handoff"] = ("PASS" if handoffs and ok_sess and ho_search else "FAIL",
                          f"count={len(handoffs)} max_tokens={max_ho_tok} search={ho_search}")
    except Exception as e:  # noqa: BLE001
        res["handoff"] = ("FAIL", repr(e))

    try:
        r_nb = st.search("depo senkronizasyon servisi şifreleme anahtarı rotasyonu", "depo-sync", budget_tokens=BUDGET)
        chunk_hit = next((x for x in r_nb.get("results", []) if x.get("source_type") != "note"), None)
        item_id = chunk_hit["id"] if chunk_hit else ""
        g = st.get(item_id, project="depo-sync", before=1, after=1) if item_id else None
        iso_g = st.get(item_id, project="kargo-api", before=1, after=1) if item_id else "bad"
        has_before = bool(g and any("SYNC_PORT=8443" in x.get("text", "") for x in g.get("before", [])))
        has_after = bool(g and any("CHACHA20_POLY1305" in x.get("text", "") for x in g.get("after", [])))
        ok = has_before and has_after and iso_g is None
        res["neighbors"] = ("PASS" if ok else "FAIL",
                            f"before={has_before} after={has_after} isolated={iso_g is None}")
    except Exception as e:  # noqa: BLE001
        res["neighbors"] = ("FAIL", repr(e))

    try:
        r = st.search("kargo takip numarası formatı", "kargo-api", budget_tokens=BUDGET)
        cx = next(i for i in r["results"] if "KRG-" in i.get("text", ""))
        r = st.search("db/engine.py SQLite geçiş", "kargo-api", budget_tokens=BUDGET)
        cc = next(i for i in r["results"] if "engine.py" in i.get("text", ""))
        n2 = st.get(ids["n2"])
        ok = (cx.get("agent"), cx.get("model")) == ("codex", "gpt-6.1-sol")
        ok = ok and (cc.get("agent"), cc.get("model")) == ("claude", "claude-opus-5-5") and n2.get("agent") == "claude-code"
        res["signature"] = ("PASS" if ok else "FAIL",
                            f"codex={cx.get('agent')}/{cx.get('model')} claude={cc.get('agent')}/{cc.get('model')} "
                            f"note={n2.get('agent')}")
    except Exception as e:  # noqa: BLE001
        res["signature"] = ("FAIL", repr(e))

    try:
        md = st.export("kargo-api")
        ok = md.lstrip().startswith("#") and all(x in md for x in ("SQLite", "PostgreSQL", "Redis", "iade"))
        ok = ok and "MongoDB" not in md and "%64" not in md and not any(s in md for s in SECRETS)
        res["export"] = ("PASS" if ok else "FAIL", f"tokens={tok(md)}")
        md_cache, md_hits, md_total = {}, 0, 0
        for q in questions:
            if q["expect"] != "ok":
                continue
            p = q["project"]
            if p not in md_cache:
                md_cache[p] = st.export(p)
            exp_strs = q.get("top1_contains", []) + q.get("any_contains", [])
            md_total += 1
            md_hits += all(low(s) in low(md_cache[p]) for s in exp_strs)
        md_toks = sum(tok(v) for v in md_cache.values())
        res["markdown_baseline"] = ("INFO", f"{md_hits}/{md_total} export_tokens={md_toks}")
    except Exception as e:  # noqa: BLE001
        res["export"] = ("FAIL", repr(e))
        res["markdown_baseline"] = ("INFO", repr(e))
    st.close()

    try:
        mig_tmp = Path(tempfile.mkdtemp())
        mig_db = mig_tmp / "r3.db"
        shutil.copyfile(FIX / "r3_schema.db", mig_db)
        mst = Store(mig_db)
        has_sv = bool(mst.con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone())
        ver_rows = mst.con.execute("SELECT version FROM schema_version").fetchall() if has_sv else []
        r_mig = mst.search("PostgreSQL", "kargo-api", budget_tokens=BUDGET)
        nid_mig = mst.save_note("kargo-api", "Post-migration write check", "fact")
        ok = has_sv and bool(ver_rows) and ver_rows[-1][0] >= 1 and r_mig.get("status") == "ok" and bool(mst.get(nid_mig))
        mst.close()
        res["migrate"] = ("PASS" if ok else "FAIL", f"schema_version={has_sv} rows={len(ver_rows)}")
    except Exception as e:  # noqa: BLE001
        res["migrate"] = ("FAIL", repr(e))

    try:
        doc_tmp = Path(tempfile.mkdtemp())
        doc_db = doc_tmp / "doc.db"
        doc_home = doc_tmp / "home"
        doc_home.mkdir()
        dst = Store(doc_db)
        dst.ingest_file(FIX / "corrupt" / "bad.jsonl", agent="claude")
        dst.close()
        env = {**os.environ, "MESA_LORE_DB": str(doc_db), "HOME": str(doc_home)}
        proc = subprocess.run(
            [sys.executable, "-m", "mesa_lore.cli", "doctor", "--home", str(doc_home)],
            env=env, capture_output=True, text=True, timeout=10,
        )
        out = low(proc.stdout + "\n" + proc.stderr)
        has_warn = ("warn" in out or "uyari" in out) and ("bad.jsonl" in out or "format" in out or "unrecognized" in out)
        ok = proc.returncode == 0 and has_warn
        res["doctor"] = ("PASS" if ok else "FAIL", f"rc={proc.returncode} warn_detected={has_warn}")
    except Exception as e:  # noqa: BLE001
        res["doctor"] = ("FAIL", repr(e))

    res["perf"] = perf()
    for f in fails:
        print("  miss:", f)
    return res


def perf():
    import random
    import time
    from mesa_lore.store import Store
    rnd = random.Random(7)
    words = ("kargo takip test dosya hata proje plan kullanıcı sistem veri sonuç kod değişiklik kontrol "
             "rota depo fatura teslimat şube adres müşteri sipariş paket durum").split()
    tmp = Path(tempfile.mkdtemp())
    tr = tmp / "big.jsonl"
    repo = checkout(tmp, "perf-proj")
    with tr.open("w", encoding="utf-8") as f:
        for n in range(10000):
            txt = " ".join(rnd.choice(words) for _ in range(rnd.randint(8, 40))) + f" kod{n}"
            f.write(json.dumps({"type": "user", "uuid": f"p{n}", "timestamp": "2026-11-07T10:00:00Z",
                                "sessionId": "perf", "cwd": str(repo),
                                "message": {"role": "user", "content": txt}}, ensure_ascii=False) + "\n")
    try:
        st = Store(tmp / "p.db")
        t0 = time.process_time()
        st.ingest_file(tr, agent="claude")
        ing = time.process_time() - t0
        lat = []
        for q in ["kargo teslimat durumu", "fatura adres hatası", "kod4242", "Kubernetes test planı",
                  "müşteri sipariş paketi", "depo rota planı", "şube kontrol sonucu", "Stripe ödeme"] * 3:
            t0 = time.process_time()
            st.search(q, "perf-proj", budget_tokens=BUDGET)
            lat.append((time.process_time() - t0) * 1000)
        st.close()
        p95 = sorted(lat)[int(len(lat) * 0.95) - 1]
        return ("PASS" if p95 <= PERF_P95_MS else "FAIL", f"ingest10k={ing:.1f}s p95={p95:.0f}ms (need <= {PERF_P95_MS})")
    except Exception as e:  # noqa: BLE001
        return ("FAIL", repr(e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--require", default="")
    ap.add_argument("--questions", default=str(HERE / "dev.jsonl"))
    a = ap.parse_args()
    res = run(Path(a.questions))
    for k in ALL + ["markdown_baseline"]:
        s, d = res.get(k, ("SKIP", "not run"))
        print(f"{k:17} {s:4} {d}")
    req = ALL if a.require == "all" else [x for x in a.require.split(",") if x and x != "markdown_baseline"]
    bad = [k for k in req if res.get(k, ("SKIP",))[0] != "PASS"]
    if bad:
        print("REQUIRED NOT PASSING:", ",".join(bad))
        sys.exit(1)


if __name__ == "__main__":
    main()
