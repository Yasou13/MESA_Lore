import json
import os
import sys
from io import StringIO
from unittest.mock import patch
from mesa_lore.cli import main
from mesa_lore.store import Store


def run_cli(args, env, capsys=None):
    with patch.dict(os.environ, env):
        with patch.object(sys, "argv", ["mesa-lore"] + args):
            main()


def test_cli_ingest_and_search_and_index(tmp_path, capsys):
    db_path = tmp_path / "cli.db"
    env = {"MESA_LORE_DB": str(db_path)}

    transcript = tmp_path / "claude.jsonl"
    lines = [
        {"type": "user", "sessionId": "s1", "cwd": "/home/dev/my-proj", "timestamp": "2026-01-01T10:00:00Z",
         "message": {"role": "user", "content": "Veritabanı olarak SQLite kullanalım."}}
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

    # Ingest
    run_cli(["ingest", str(transcript), "--agent", "claude"], env)
    out = capsys.readouterr().out
    assert "Ingested" in out or "chunks" in out

    # Search
    run_cli(["search", "SQLite", "--project", "my-proj"], env)
    out = capsys.readouterr().out
    assert "SQLite" in out

    # Index
    run_cli(["index", "--project", "my-proj"], env)
    out = capsys.readouterr().out
    assert "SQLite" in out


def test_cli_list_show_delete_purge(tmp_path, capsys):
    db_path = tmp_path / "cli.db"
    env = {"MESA_LORE_DB": str(db_path)}

    st = Store(db_path)
    n1 = st.save_note("my-proj", "Note 1 alpha text", "decision")
    n2 = st.save_note("other-proj", "Note 2 beta text", "fact")
    ng = st.save_note("global", "Global note text", "fact")
    # Cross-project replaces must not supersede n1
    st.save_note("other-proj", "Attempt to replace n1", "decision", replaces=n1)
    assert st.get(n1)["state"] == "active"
    assert st.get(n2, project="my-proj") is None
    assert st.get(ng, project="my-proj")["text"] == "Global note text"
    st.close()

    # `list` was merged into `view`: search keeps this scope check (my-proj and global, not other-proj)
    run_cli(["search", "note text", "--project", "my-proj"], env)
    out = capsys.readouterr().out
    assert n1 in out
    assert ng in out
    assert n2 not in out

    # Show with project isolation
    run_cli(["show", n1, "--project", "my-proj"], env)
    out = capsys.readouterr().out
    assert "Note 1 alpha text" in out

    run_cli(["show", ng, "--project", "my-proj"], env)
    out = capsys.readouterr().out
    assert "Global note text" in out

    run_cli(["show", n2, "--project", "my-proj"], env)
    out = capsys.readouterr().out
    assert "Not found" in out

    # Delete
    run_cli(["delete", n1], env)
    out = capsys.readouterr().out
    assert "Deleted" in out

    # Verify deleted (both entries and entries_fts, and no longer findable via search)
    st = Store(db_path)
    assert st.get(n1) is None
    assert st.con.execute("SELECT COUNT(*) FROM entries_fts WHERE id = ?", (n1,)).fetchone()[0] == 0
    assert st.search("Note 1 alpha", "my-proj")["status"] == "no_match"
    assert st.get(n2) is not None
    st.close()

    # Purge
    run_cli(["purge", "--project", "other-proj"], env)
    out = capsys.readouterr().out
    assert "Purged" in out

    st = Store(db_path)
    assert st.get(n2) is None
    assert st.con.execute("SELECT COUNT(*) FROM entries_fts WHERE id = ?", (n2,)).fetchone()[0] == 0
    assert st.search("Note 2 beta", "other-proj")["status"] == "no_match"
    st.close()


def test_cli_ingest_pending(tmp_path, capsys):
    db_path = tmp_path / "cli.db"
    queue_path = tmp_path / "queue.txt"
    env = {"MESA_LORE_DB": str(db_path), "MESA_LORE_QUEUE": str(queue_path)}

    transcript = tmp_path / "pending.jsonl"
    transcript.write_text(
        '{"type": "user", "sessionId": "s2", "cwd": "/home/dev/app", "timestamp": "2026-01-01", "message": {"content": "Pending chunk"}}\n',
        encoding="utf-8"
    )
    queue_path.write_text(str(transcript) + "\n", encoding="utf-8")

    run_cli(["ingest-pending"], env)
    out = capsys.readouterr().out
    assert "Ingested" in out

    # Queue should be empty now
    assert queue_path.read_text().strip() == ""
