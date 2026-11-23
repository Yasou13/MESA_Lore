import json

from mesa_lore.store import Store


def _claude_tool_transcript(path, session, output):
    lines = [
        {"type": "assistant", "sessionId": session, "cwd": "/r/p", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"model": "m", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                "input": {"command": "cat f"}}]}},
        {"type": "user", "sessionId": session, "cwd": "/r/p", "timestamp": "2026-01-01T00:00:01Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": output}]}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


def test_trimmed_tool_output_keeps_no_part_of_a_secret(tmp_path):
    # The head/tail trim must not cut a secret so that its remainder no longer looks like one.
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    key_body = "\n".join("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" + str(i % 10) * 32 for i in range(40))
    cases = {
        "token": "x" * 1500 + " " + token + " " + "y" * 780,
        "pkey": "log line\n" * 300 + "-----BEGIN PRIVATE KEY-----\n" + key_body + "\n-----END PRIVATE KEY-----\n",
    }
    st = Store(tmp_path / "lore.db")
    for name, output in cases.items():
        p = tmp_path / f"{name}.jsonl"
        _claude_tool_transcript(p, name, output)
        assert st.ingest_file(p, "claude") > 0
    stored = "\n".join(r[0] for r in st.con.execute("SELECT text FROM entries"))
    assert token[-12:] not in stored
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" not in stored
    assert "END PRIVATE KEY" not in stored
    st.close()


def test_claude_subagent_file_sharing_session_id_keeps_all_chunks(tmp_path):
    # Claude Code writes subagent transcripts to <session>/subagents/agent-*.jsonl with the parent's sessionId.
    def write(path, texts):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(
            {"type": "user", "sessionId": "s1", "cwd": "/r/p", "timestamp": f"2026-01-01T00:00:0{i}Z",
             "message": {"role": "user", "content": t}}) for i, t in enumerate(texts)), encoding="utf-8")
    parent, sub = tmp_path / "s1.jsonl", tmp_path / "s1" / "subagents" / "agent-a1.jsonl"
    write(parent, ["parent asks about caching", "parent asks about logging"])
    write(sub, ["subagent explores the parser", "subagent reports findings"])
    st = Store(tmp_path / "lore.db")
    assert st.ingest_file(parent, "claude") == 2
    assert st.ingest_file(sub, "claude") == 2
    assert st.ingest_file(sub, "claude") == 0
    texts = {r[0] for r in st.con.execute("SELECT text FROM entries WHERE session_id = 's1'")}
    assert {"parent asks about logging", "subagent reports findings"} <= texts
    st.close()


def test_search_stems_only_candidate_words_that_can_match(tmp_path, monkeypatch):
    # Stemming every word of up to 200 candidates dominated search time on a real 20k-chunk DB.
    import mesa_lore.store as store_mod
    calls = []
    real = store_mod.tr_stem
    monkeypatch.setattr(store_mod, "tr_stem", lambda w: calls.append(w) or real(w))
    st = Store(tmp_path / "lore.db")
    filler = " ".join(f"word{i}x" for i in range(300))
    st.save_note("p", f"{filler} kararlarımızdan biri değişikliği", "fact")
    r = st.search("kararlarımız değişiklik", "p")
    assert r["status"] == "ok"
    assert len(calls) < 30
    st.close()


def test_english_words_are_not_dropped_as_inflected_stopwords(tmp_path):
    # Turkish suffix stripping turned "notes" into "not" and "theme" into "the"; only Turkish stopwords inflect.
    st = Store(tmp_path / "lore.db")
    st.save_note("p", "Release notes live in docs/CHANGES.md", "fact")
    st.save_note("p", "The dark theme uses the slate palette", "decision")
    assert st.search("notes", "p")["status"] == "ok"
    assert st.search("theme", "p")["status"] == "ok"
    assert st.search("nerede", "p")["status"] == "no_match"  # inflected Turkish stopword still ignored
    st.close()


def test_masking_is_fast_on_long_word_runs():
    # The key=value pattern retried from every position inside a word run (quadratic); whole tool
    # outputs are masked before trimming, so this must stay linear.
    import time
    from mesa_lore.text import mask_secrets
    text = "a" * 60_000 + " api_key=abcd1234"
    t0 = time.perf_counter()
    assert mask_secrets(text).endswith("[REDACTED]")
    assert time.perf_counter() - t0 < 1.0


def test_k_form_query_finds_softened_g_form(tmp_path):
    # k↔ğ softening only ran ğ→k, so a query in the k-form never fetched text that only has the ğ-form.
    st = Store(tmp_path / "lore.db")
    st.save_note("p", "Bu değişikliği geri aldık", "fact")
    st.save_note("p", "Yeni renk paleti", "fact")
    assert st.search("değişiklik", "p")["status"] == "ok"
    assert st.search("değişikliklerden", "p")["status"] == "ok"
    assert st.search("rengi", "p")["status"] == "ok"  # existing ğ/g→k direction
    st.close()
