import json

from mesa_lore import mcp_server
from mesa_lore.store import SCHEMA_VERSION, Store, get_db_path

HANDOFF = "Done: shipped parser\nNext: write the README"


def transcript(tmp_path, nid, sid="s1", tool="mcp__mesa-lore__memory_save", name="t.jsonl", kind="handoff"):
    repo = tmp_path / "app"
    repo.mkdir(exist_ok=True)
    base = {"sessionId": sid, "cwd": str(repo), "timestamp": "2026-11-23T10:00:00Z"}
    lines = [
        {**base, "type": "user", "message": {"role": "user", "content": "ship the parser"}},
        {**base, "type": "assistant", "message": {"role": "assistant", "id": "m1", "content": [
            {"type": "tool_use", "id": "t1", "name": tool, "input": {"kind": kind, "text": HANDOFF, "session_id": sid}}]}},
        {**base, "type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": f"<untrusted_memory>\nSaved note: {nid} (kind: {kind}, project: app)\n</untrusted_memory>"}]}},
    ]
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    return path


def test_ingest_links_handoff_saved_without_session(tmp_path):
    st = Store(get_db_path())
    nid = st.save_note("app", HANDOFF, "handoff", agent="claude")
    st.ingest_file(transcript(tmp_path, nid), "claude")
    assert st.get(nid)["session_id"] == "s1"
    sessions = st.sections("app")["Sessions"]
    assert len(sessions) == 1 and sessions[0]["id"] == nid and sessions[0]["asked"] == "ship the parser"
    assert "Next: write the README" in st.export("app")
    st.close()


def test_migration_links_existing_rows(tmp_path):
    st = Store(get_db_path())
    nid = st.save_note("app", HANDOFF, "handoff", agent="claude")
    st.ingest_file(transcript(tmp_path, nid), "claude")
    with st.con:
        st.con.execute("UPDATE entries SET session_id = NULL WHERE id = ?", (nid,))
        st.con.execute("DELETE FROM schema_version WHERE version >= 12")
    st.close()
    st = Store(get_db_path())
    assert SCHEMA_VERSION >= 12 and st._current_schema_version() == SCHEMA_VERSION
    assert st.get(nid)["session_id"] == "s1"
    st.close()


def test_other_tool_output_cannot_link_a_handoff(tmp_path):
    st = Store(get_db_path())
    nid = st.save_note("app", HANDOFF, "handoff")
    st.ingest_file(transcript(tmp_path, nid, tool="Bash"), "claude")
    assert st.get(nid)["session_id"] is None
    st.close()


def test_link_keeps_existing_session_and_skips_other_kinds(tmp_path):
    st = Store(get_db_path())
    kept = st.save_note("app", HANDOFF, "handoff", session_id="original")
    fact = st.save_note("app", "A fact", "fact")
    st.ingest_file(transcript(tmp_path, kept), "claude")
    st.ingest_file(transcript(tmp_path, fact, sid="s2", name="f.jsonl"), "claude")
    assert st.get(kept)["session_id"] == "original" and st.get(fact)["session_id"] is None
    st.close()


def test_unlinked_handoff_still_reaches_index_and_export():
    st = Store(get_db_path())
    nid = st.save_note("app", "Done: a handoff with no transcript yet\nNext: keep going", "handoff", agent="claude")
    assert "a handoff with no transcript yet" in st.index("app")
    assert "Next: keep going" in st.export("app")
    assert st.latest_handoff("app")["id"] == nid
    st.close()


def test_memory_save_stores_the_session_id():
    out = mcp_server.memory_save(text=HANDOFF, kind="handoff", project="app", session_id="s7")
    nid = out.split("Saved note: ")[1].split()[0]
    st = Store(get_db_path())
    assert st.get(nid)["session_id"] == "s7"
    st.close()
