import sys
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
