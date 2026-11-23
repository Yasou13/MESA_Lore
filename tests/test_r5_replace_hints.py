import re
from mesa_lore.store import Store


def _states(st, *ids):
    return [st.get(i)["state"] for i in ids]


def test_same_topic_is_suggested_but_never_superseded(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        old = st.save_note("proj", "Veritabanı motoru olarak PostgreSQL kullanılacak.", "decision")
        other = st.save_note("proj", "Kimlik doğrulama için OAuth2 PKCE zorunlu.", "decision")
        todo = st.save_note("proj", "Kargo iade akışı için endpoint yazılacak.", "todo")
        new_text = "Veritabanı motoru olarak SQLite seçildi."
        assert st.replace_hints("proj", new_text, "decision") == [old]
        new = st.save_note("proj", new_text, "decision")
        assert _states(st, old, other, new) == ["active"] * 3  # saving alone changes nothing
        assert st.replace_hints("proj", "Kargo iade akışı endpoint tamamlandı ve devreye alındı.", "fact") == [todo]
        assert st.get(todo)["state"] == "active"
        assert st.replace_hints("other-proj", new_text, "decision") == []  # same project only
    finally:
        st.close()


def test_login_timeout_fix_does_not_touch_jwt_decision(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        jwt = st.save_note("app", "Use JWT for login", "decision")
        jwt_fact = st.save_note("app", "Login uses JWT tokens signed with RS256", "fact")
        text = "Fixed the login timeout"
        assert st.replace_hints("app", text, "fact") == []
        assert st.replace_hints("app", text, "decision") == []
        st.save_note("app", text, "fact")
        assert _states(st, jwt, jwt_fact) == ["active", "active"]
    finally:
        st.close()


def test_hint_needs_a_change_cue_and_enough_overlap(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        st.save_note("app", "Cache layer uses Redis with a 5 minute TTL", "decision")
        assert st.replace_hints("app", "Cache layer uses Redis cluster mode", "decision") == []  # no cue word
        assert st.replace_hints("app", "Switched the logging format to JSON lines", "decision") == []  # cue, other topic
        assert st.replace_hints("app", "Cache layer switched from Redis to Dragonfly", "decision") != []
        assert st.replace_hints("app", "Cache layer switched from Redis to Dragonfly", "todo") == []  # todo/handoff never hint
    finally:
        st.close()


def test_mcp_save_returns_hint_and_agent_decides_via_replaces(tmp_path, monkeypatch):
    from mesa_lore import mcp_server
    monkeypatch.setenv("MESA_LORE_DB", str(tmp_path / "m.db"))
    old = re.search(r"Saved note: (\S+)", mcp_server.memory_save("Önbellek motoru olarak Redis kullanılacak.", kind="decision", project="app")).group(1)
    text = "Önbellek motoru olarak Redis yerine Dragonfly seçildi."
    out = mcp_server.memory_save(text, kind="decision", project="app")
    new = re.search(r"Saved note: (\S+)", out).group(1)
    assert f"possibly replaces: {old}" in out
    st = Store(tmp_path / "m.db")
    assert _states(st, old, new) == ["active", "active"]
    st.close()
    # the agent agrees: re-saving the same text with replaces links the existing note instead of duplicating it
    out2 = mcp_server.memory_save(text, kind="decision", project="app", replaces=old)
    assert f"Saved note: {new} " in out2 and "possibly replaces" not in out2
    st = Store(tmp_path / "m.db")
    try:
        assert _states(st, old, new) == ["superseded", "active"]
        assert st.con.execute("SELECT count(*) FROM entries WHERE text = ?", (text,)).fetchone()[0] == 1
        res = st.search("önbellek motoru", "app")["results"]
        assert [r["id"] for r in res][:2] == [new, old]  # the replaced note still shows, after its successor
    finally:
        st.close()
