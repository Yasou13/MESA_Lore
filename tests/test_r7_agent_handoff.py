import json

from mesa_lore.store import Store


def _session(tmp_path, sid, day):
    lines = [{"type": "user", "sessionId": sid, "cwd": "/repo/app", "timestamp": f"2026-11-{day}T10:00:00Z",
              "message": {"role": "user", "content": f"walrus migration request for {sid}"}},
             {"type": "assistant", "sessionId": sid, "cwd": "/repo/app", "timestamp": f"2026-11-{day}T10:00:05Z",
              "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": f"cpu summary walrus migration {sid} finished"}]}}]
    p = tmp_path / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def _handoff_hits(st, query="walrus migration"):
    return [r["text"] for r in st.search(query, "app", budget_tokens=800, kind="handoff")["results"]]


def test_no_agent_handoff_shows_cpu_handoff(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(_session(tmp_path, "s1", 10), "claude")
        assert "cpu summary walrus" in st.index("app")
        assert any("cpu summary walrus" in t for t in _handoff_hits(st))
    finally:
        st.close()


def test_newest_agent_handoff_replaces_cpu_handoff_in_index_and_search(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(_session(tmp_path, "s1", 10), "claude")
        st.save_note("app", "Done: old walrus migration step\nNext: first", "handoff", session_id="s1")
        st.save_note("app", "Done: new walrus migration step\nNext: second", "handoff", session_id="s1")
        idx = st.index("app")
        assert "Done: new walrus" in idx and "old walrus" not in idx and "cpu summary" not in idx
        assert idx.count("\n- 2026") == 1  # one line for the session
        hits = _handoff_hits(st)
        assert len(hits) == 1 and "new walrus" in hits[0]
        assert not any("cpu summary" in r["text"] and r["kind"] == "handoff" for r in st.search("walrus migration", "app")["results"])
    finally:
        st.close()


def test_agent_handoff_replaces_only_its_own_session(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(_session(tmp_path, "s1", 10), "claude")
        st.ingest_file(_session(tmp_path, "s2", 11), "claude")
        st.save_note("app", "Done: agent walrus migration for s1", "handoff", session_id="s1")
        idx = st.index("app")
        assert "Done: agent walrus" in idx and "cpu summary walrus migration s2" in idx
        assert "cpu summary walrus migration s1" not in idx
    finally:
        st.close()


def test_cpu_handoff_returns_when_agent_handoff_is_deleted(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(_session(tmp_path, "s1", 10), "claude")
        nid = st.save_note("app", "Done: agent walrus migration", "handoff", session_id="s1")
        assert st.delete(nid)
        assert "cpu summary walrus" in st.index("app")
        assert any("cpu summary walrus" in t for t in _handoff_hits(st))
    finally:
        st.close()


def test_handoff_without_session_does_not_hide_cpu_handoffs(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(_session(tmp_path, "s1", 10), "claude")
        st.save_note("app", "Done: sessionless walrus migration", "handoff")
        assert "cpu summary walrus" in st.index("app")
        hits = _handoff_hits(st)
        assert any("cpu summary" in t for t in hits) and any("sessionless" in t for t in hits)
    finally:
        st.close()


def test_agent_handoff_of_other_project_is_not_shown(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(_session(tmp_path, "s1", 10), "claude")
        st.save_note("other", "Done: foreign walrus migration", "handoff", session_id="s1")
        assert "foreign" not in st.index("app") and "cpu summary walrus" in st.index("app")
        assert not any("foreign" in t for t in _handoff_hits(st))
    finally:
        st.close()
