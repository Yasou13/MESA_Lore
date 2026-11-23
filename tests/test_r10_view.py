import io, re, sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mesa_lore import cli
from mesa_lore.store import Store, get_db_path

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc).timestamp()
EMPTY = "No memory yet. Run `mesa-lore install`.\n"


@pytest.fixture
def run(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "60")
    monkeypatch.setattr(cli, "time", SimpleNamespace(time=lambda: NOW))

    def _run(*argv, columns=60):
        monkeypatch.setenv("COLUMNS", str(columns))
        monkeypatch.setattr(sys, "argv", ["mesa-lore", *argv])
        cli.main()
        return capsys.readouterr().out
    return _run


def note(st, project, text, kind, ts, nid, **kw):
    old = st.save_note(project, text, kind, **kw)
    with st.con:
        st.con.execute("UPDATE entries SET id = ?, ts = ? WHERE id = ?", (nid, ts, old))
        st.con.execute("UPDATE entries_fts SET id = ? WHERE id = ?", (nid, old))
    return nid


def auto_handoff(st, project, sid, text, agent, ts):
    with st.con:
        st.con.execute("INSERT INTO handoffs (id, project, text, session_id, ts, agent) VALUES (?, ?, ?, ?, ?, ?)",
                       (f"h_{project}_{sid}", project, text, sid, ts, agent))


def seed():
    st = Store(get_db_path())
    note(st, "global", "Prefer small commits", "fact", "2026-11-20T10:00:00+00:00", "n_global0001")
    note(st, "alpha", "Done: shipped parser\nDecisions: keep sqlite\nOpen: docs\nNext: write the README", "handoff",
         "2026-11-23T10:00:00+00:00", "n_handoff001", session_id="s1", agent="claude")
    auto_handoff(st, "beta", "s2", "Asked: fix login\nDone: fixed the login bug", "codex", "2026-11-20T12:00:00Z")
    note(st, "research:rag", "Chunks near 250 tokens work", "fact", "2026-11-13T12:00:00+00:00", "n_research01")
    return st


def test_overview_golden(run):
    seed().close()
    assert run("view") == (
        "YOU\n"
        "  Prefer small commits\n"
        "\n"
        "PROJECTS\n"
        "  alpha  Next: write the README         claude · 2 hours ago\n"
        "  beta   Done: fixed the login bug        codex · 3 days ago\n"
        "\n"
        "RESEARCH\n"
        "  research:rag                                   10 days ago\n"
    )


def test_overview_sorts_by_last_activity(run):
    st = seed()
    auto_handoff(st, "beta", "s3", "Asked: later\nDone: newer work", "codex", "2026-11-23T11:59:00Z")
    st.close()
    rows = run("view").split("PROJECTS\n")[1].splitlines()
    assert rows[0].startswith("  beta") and rows[0].endswith("codex · 1 minute ago") and rows[1].startswith("  alpha")


def test_agent_handoff_without_session_is_used(run):
    st = Store(get_db_path())
    auto_handoff(st, "gamma", "s9", "Asked: old\nDone: older auto", "codex", "2026-11-23T09:00:00Z")
    note(st, "gamma", "Done: d\nNext: from the agent", "handoff", "2026-11-23T11:00:00+00:00", "n_loose000001", agent="claude")
    st.close()
    assert "Next: from the agent" in run("view")


def test_empty_db_message_and_no_file_created(run):
    assert run("view") == EMPTY
    assert not get_db_path().exists()
    Store(get_db_path()).close()
    assert run("view") == EMPTY


@pytest.mark.parametrize("columns", [40, 60, 133])
def test_width_cut(run, columns):
    st = seed()
    auto_handoff(st, "a-very-long-project-name-that-needs-cutting", "s4", "Done: " + "uzun metin " * 60, "local-agent-mode-mesa-lore", "2026-11-23T11:00:00Z")
    note(st, "global", "global " * 80, "fact", "2026-11-21T10:00:00+00:00", "n_global0002")
    st.close()
    out = run("view", columns=columns)
    assert "…" in out and all(len(line) <= columns for line in out.splitlines())


def test_width_counts_wide_characters(run):
    st = Store(get_db_path())
    auto_handoff(st, "wide", "s5", "Done: " + "完了" * 80, "claude", "2026-11-23T11:00:00Z")
    st.close()
    row = next(line for line in run("view").splitlines() if line.startswith("  wide"))
    assert sum(2 if "一" <= c <= "鿿" else 1 for c in row) <= 60


def test_view_masks_secrets_before_cutting(run):
    secret = "sk-ant-api03-FAKEKEY1234567890abcdefXYZ"
    st = Store(get_db_path())
    auto_handoff(st, "leaky", "s6", f"Done: {'x' * 20} {secret} tail", "claude", "2026-11-23T11:00:00Z")
    st.close()
    out = run("view", columns=52)
    assert "sk-ant" not in out and "FAKEKEY" not in out


def test_view_strips_terminal_control_characters(run):
    st = Store(get_db_path())
    auto_handoff(st, "ctrl", "s7", "Done: safe\x1b[2Jtext\x07 here", "claude", "2026-11-23T11:00:00Z")
    st.close()
    out = run("view")
    assert "\x1b" not in out and "\x07" not in out and "safe [2Jtext here" in out


def seed_project():
    st = Store(get_db_path())
    note(st, "alpha", "Write the docs", "todo", "2026-11-20T09:00:00+00:00", "n_todo000001")
    note(st, "alpha", "Add a release note", "todo", "2026-11-21T09:00:00+00:00", "n_todo000002")
    note(st, "alpha", "Keep SQLite as the only store", "decision", "2026-11-19T09:00:00+00:00", "n_deci000001")
    note(st, "alpha", "Done: shipped parser\nDecisions: keep sqlite\nOpen: docs\nNext: write the README", "handoff",
         "2026-11-23T10:00:00+00:00", "n_handoff001", session_id="s3", agent="claude")
    auto_handoff(st, "alpha", "s3", "Asked: ship the parser\nDone: auto text", "claude", "2026-11-23T09:59:00Z")
    auto_handoff(st, "alpha", "s2", "Asked: fix login\nDone: fixed the login bug\nOpen: stale copy", "codex", "2026-11-22T08:00:00Z")
    auto_handoff(st, "other", "s9", "Asked: secret other\nDone: other project", "codex", "2026-11-23T11:00:00Z")
    return st


PROJECT_GOLDEN = (
    "alpha\n"
    "\n"
    "PICK UP WHERE YOU LEFT OFF\n"
    "  claude · written by agent · 2 hours ago\n"
    "  Done       shipped parser\n"
    "  Decisions  keep sqlite\n"
    "  Open       docs\n"
    "  Next       write the README\n"
    "\n"
    "OPEN ITEMS\n"
    "  1. Write the docs\n"
    "  2. Add a release note\n"
    "\n"
    "DECISIONS\n"
    "  n_deci00  Keep SQLite as the only store\n"
    "\n"
    "SESSIONS\n"
    "  2026-11-23  claude\n"
    "    Asked  ship the parser\n"
    "    Done   shipped parser\n"
    "  2026-11-22  codex\n"
    "    Asked  fix login\n"
    "    Done   fixed the login bug\n"
)


def test_project_view_golden(run):
    seed_project().close()
    assert run("view", "alpha") == PROJECT_GOLDEN


def test_project_view_defaults_to_current_folder(run, monkeypatch):
    seed_project().close()
    monkeypatch.setattr(cli, "get_project", lambda cwd: "alpha")
    assert run("view", ".") == PROJECT_GOLDEN


def test_auto_handoff_shows_live_open_item(run):
    st = Store(get_db_path())
    note(st, "beta", "Current todo", "todo", "2026-11-20T09:00:00+00:00", "n_todo000009")
    auto_handoff(st, "beta", "s1", "Asked: a\nDone: b\nOpen: stale copy", "codex", "2026-11-23T11:00:00Z")
    st.close()
    out = run("view", "beta")
    assert "codex · auto · 1 hour ago" in out and "Open       Current todo" in out and "stale copy" not in out


def test_sessions_limit(run):
    st = Store(get_db_path())
    for i in range(7):
        auto_handoff(st, "many", f"s{i}", f"Asked: ask{i}\nDone: done{i}", "claude", f"2026-11-1{i}T10:00:00Z")
    st.close()
    assert run("view", "many").count("Asked") == 5
    assert run("view", "many", "--sessions", "7").count("Asked") == 7


def test_unknown_project(run):
    seed().close()
    assert run("view", "nope") == "No memory for nope.\n"


def active_todos(project="alpha"):
    st = Store(get_db_path())
    try:
        return [n["text"] for n in st.sections(project, 0)["Open items"]]
    finally:
        st.close()


def row(nid):
    st = Store(get_db_path())
    try:
        return st.get(nid)
    finally:
        st.close()


def test_note_add_defaults_to_todo_signed_user(run):
    out = run("note", "add", "Write tests", "-p", "alpha")
    assert re.fullmatch(r"Added n_[0-9a-f]{6} to alpha\.\n", out)
    st = Store(get_db_path())
    n = st.sections("alpha", 0)["Open items"][0]
    st.close()
    assert (n["text"], n["agent"], n["kind"]) == ("Write tests", "user", "todo")


def test_note_add_kind_and_current_project(run, monkeypatch):
    monkeypatch.setattr(cli, "get_project", lambda cwd: "alpha")
    run("note", "add", "Use WAL", "--kind", "decision")
    assert "Use WAL" in run("view", "alpha").split("DECISIONS\n")[1]


def test_note_add_masks_secrets_and_rejects_empty(run):
    run("note", "add", "key sk-ant-api03-FAKEKEY1234567890abcdefXYZ", "-p", "alpha")
    assert "FAKEKEY" not in active_todos()[0]
    with pytest.raises(SystemExit):
        run("note", "add", "   ", "-p", "alpha")
    assert len(active_todos()) == 1


def test_note_done_by_number_hides_and_renumbers(run):
    seed_project().close()
    assert run("note", "done", "1", "-p", "alpha") == "Done: Write the docs\n"
    assert run("view", "alpha").split("OPEN ITEMS\n")[1].startswith("  1. Add a release note\n\nDECISIONS")
    assert row("n_todo000001")["state"] == "superseded"


def test_note_done_by_id_and_number_uses_current_project(run, monkeypatch):
    seed_project().close()
    st = Store(get_db_path())
    note(st, "other", "Other todo", "todo", "2026-11-19T09:00:00+00:00", "n_othr000001")
    st.close()
    monkeypatch.setattr(cli, "get_project", lambda cwd: "other")
    run("note", "done", "1")
    assert row("n_othr000001")["state"] == "superseded" and row("n_todo000001")["state"] == "active"
    monkeypatch.setattr(cli, "get_project", lambda cwd: "alpha")
    run("note", "done", "n_todo000002")
    assert active_todos() == ["Write the docs"]


@pytest.mark.parametrize("ref", ["0", "3", "n_deci000001", "n_missing000"])
def test_note_done_refuses_bad_targets(run, ref):
    seed_project().close()
    with pytest.raises(SystemExit) as e:
        run("note", "done", ref, "-p", "alpha")
    assert e.value.code and len(active_todos()) == 2 and row("n_deci000001")["state"] == "active"


def test_short_id_works_and_ambiguous_is_refused(run):
    seed_project().close()
    assert run("note", "rm", "n_deci00", "-y", "-p", "alpha") == "Deleted n_deci00.\n"
    with pytest.raises(SystemExit) as e:
        run("note", "done", "n_todo00", "-p", "alpha")
    assert "Ambiguous" in str(e.value) and len(active_todos()) == 2


def test_note_edit_replaces_and_keeps_history(run):
    seed_project().close()
    out = run("note", "edit", "n_todo000001", "Write the full docs", "-p", "alpha")
    assert re.fullmatch(r"Replaced n_todo00 with n_[0-9a-f]{6}\.\n", out)
    assert active_todos() == ["Add a release note", "Write the full docs"]
    assert row("n_todo000001")["state"] == "superseded"
    st = Store(get_db_path())
    history = st.export("alpha").split("## History")[1]
    new = next(n for n in st.sections("alpha", 0)["Open items"] if n["text"] == "Write the full docs")
    st.close()
    assert "Write the docs" in history and new["agent"] == "user"


def test_note_edit_keeps_global_scope(run):
    seed().close()
    run("note", "edit", "n_global0001", "Prefer tiny commits", "-p", "alpha")
    assert "Prefer tiny commits" in run("view").split("PROJECTS")[0]


@pytest.mark.parametrize("nid", ["n_handoff001", "n_todo000001"])
def test_note_edit_refuses_handoff_and_superseded(run, nid):
    seed_project().close()
    run("note", "done", "n_todo000001", "-p", "alpha")
    with pytest.raises(SystemExit):
        run("note", "edit", nid, "new text", "-p", "alpha")
    assert row(nid)["text"] != "new text"


@pytest.mark.parametrize("answer, kept", [("n\n", True), ("\n", True), ("", True), ("y\n", False), ("YES\n", False)])
def test_note_rm_asks_first(run, monkeypatch, answer, kept):
    seed_project().close()
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    out = run("note", "rm", "n_todo000001", "-p", "alpha")
    assert out.startswith("Delete Write the docs? [y/N] ")
    assert out.endswith("Kept.\n" if kept else "Deleted n_todo00.\n")
    assert (row("n_todo000001") is not None) == kept


def test_note_rm_yes_does_not_prompt(run, monkeypatch):
    seed_project().close()
    monkeypatch.setattr(sys, "stdin", None)
    assert run("note", "rm", "n_todo000001", "-y", "-p", "alpha") == "Deleted n_todo00.\n"
    assert row("n_todo000001") is None


def test_note_rm_stays_in_scope_and_on_notes(run):
    seed_project().close()
    st = Store(get_db_path())
    note(st, "other", "Not yours", "fact", "2026-11-19T09:00:00+00:00", "n_othr000001")
    with st.con:
        st.con.execute("INSERT INTO entries (id, project, text, source_type) VALUES ('c_chunk00001', 'alpha', 'chunk', 'user')")
    st.close()
    for ref in ("n_othr000001", "c_chunk00001", "h_alpha_s2"):
        with pytest.raises(SystemExit):
            run("note", "rm", ref, "-y", "-p", "alpha")
    assert row("n_othr000001") and row("c_chunk00001") and row("h_alpha_s2")


def strip_ansi(s):
    return re.sub(r"\x1b\[\d+m", "", s)


def test_colors_on_a_tty_keep_the_golden_text(run, monkeypatch):
    seed_project().close()
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    out = run("view", "alpha")
    assert "\x1b[1mOPEN ITEMS\x1b[0m" in out and "\x1b[2mDone" in out and "\x1b[36mNext" in out
    assert strip_ansi(out) == PROJECT_GOLDEN


def test_overview_colors_do_not_change_alignment(run, monkeypatch):
    seed().close()
    plain = run("view")
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    out = run("view")
    assert "\x1b[1mPROJECTS\x1b[0m" in out and "\x1b[36mNext" in out and strip_ansi(out) == plain


def test_no_color_env_wins_on_a_tty(run, monkeypatch):
    seed_project().close()
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert "\x1b" not in run("view", "alpha")


def test_no_colors_when_piped(run, monkeypatch):
    seed_project().close()
    monkeypatch.delenv("NO_COLOR")
    assert "\x1b" not in run("view", "alpha")
