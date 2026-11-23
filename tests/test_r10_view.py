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
