import os
import sys
from unittest.mock import patch
from mesa_lore.cli import main
from mesa_lore.store import Store


def run_cli(args, env):
    with patch.dict(os.environ, env):
        with patch.object(sys, "argv", ["mesa-lore"] + args):
            main()


def test_store_export(tmp_path):
    db_path = tmp_path / "export_test.db"
    st = Store(db_path)

    # Project notes
    n1 = st.save_note(
        "my-proj",
        "We decided to use Redis",
        "decision",
        agent="claude-code",
    )
    n2 = st.save_note(
        "my-proj",
        "Redis was replaced with Memcached",
        "decision",
        replaces=n1,
        agent="codex",
    )
    st.save_note(
        "my-proj",
        "Tried using Mongo but failed due to memory limits",
        "failed_attempt",
        agent="claude-code",
    )
    st.save_note(
        "my-proj",
        "Remember to update auth credentials sk-ant-api03-abcdef1234567890abcdef1234567890",
        "todo",
        agent="codex",
    )
    st.save_note(
        "my-proj",
        "Backend runs on Python 3.12",
        "fact",
        agent="claude-code",
    )

    # Notes in other projects
    st.save_note(
        "other-proj",
        "Note in other project that must not leak",
        "fact",
        agent="other-agent",
    )
    st.save_note(
        "global",
        "Global preference note that must not be in project export",
        "fact",
        agent="user",
    )

    md = st.export("my-proj")

    # Header
    assert md.startswith("# my-proj")

    # Sections
    assert "## Decisions" in md
    assert "## Failed Attempts" in md
    assert "## Todos" in md
    assert "## Facts" in md
    assert "## History" in md

    # Contents
    assert "[codex] Redis was replaced with Memcached" in md
    assert "[claude-code] Tried using Mongo but failed" in md
    assert "[claude-code] Backend runs on Python 3.12" in md
    assert "[claude-code] We decided to use Redis" in md  # In History

    # Secrets masked
    assert "sk-ant-api03" not in md
    assert "[REDACTED]" in md

    # Isolation
    assert "other project that must not leak" not in md
    assert "Global preference note" not in md

    st.close()


def test_cli_export(tmp_path, capsys):
    db_path = tmp_path / "cli_export.db"
    env = {"MESA_LORE_DB": str(db_path)}

    st = Store(db_path)
    st.save_note("proj-a", "Decision 1", "decision", agent="agent-x")
    st.close()

    # Export to stdout
    run_cli(["export", "--project", "proj-a"], env)
    out = capsys.readouterr().out
    assert "# proj-a" in out
    assert "## Decisions" in out
    assert "[agent-x] Decision 1" in out

    # Export to file
    out_file = tmp_path / "exported.md"
    run_cli(["export", "--project", "proj-a", "--out", str(out_file)], env)
    assert out_file.exists()
    file_content = out_file.read_text(encoding="utf-8")
    assert file_content == out
