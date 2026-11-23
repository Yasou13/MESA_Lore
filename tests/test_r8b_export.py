import hashlib, json, os, sys
from unittest.mock import patch
import pytest
from mesa_lore.cli import main
from mesa_lore.store import Store

SECRET = "sk-ant-api03-abcdef1234567890abcdef1234567890"


def run_cli(args, db, home):
    env = {"MESA_LORE_DB": str(db), "HOME": str(home)}
    with patch.dict(os.environ, env), patch.object(sys, "argv", ["mesa-lore", *args]):
        main()


def add_cpu_handoff(st, project, sid, ts, text, agent="codex"):
    with st.con:
        st.con.execute("INSERT INTO handoffs (id, project, text, session_id, ts, agent) VALUES (?, ?, ?, ?, ?, ?)",
                       (f"h_{project}_{sid}", project, text, sid, ts, agent))


def db_digest(db):  # SQLite may leave an empty -wal and a -shm index beside a WAL file opened read-only
    wal = db.with_name(f"{db.name}-wal")
    return hashlib.sha256(db.read_bytes()).hexdigest(), wal.stat().st_size if wal.exists() else 0


def seed(db):
    st = Store(db)
    add_cpu_handoff(st, "app", "old", "2026-11-01T10:00:00Z", f"İstek: old cpu\nYapılan: used {SECRET}")
    add_cpu_handoff(st, "app", "mid", "2026-11-05T10:00:00Z", "cpu text replaced by agent")
    add_cpu_handoff(st, "app", "new", "2026-11-09T10:00:00Z", "newest cpu", "claude")
    add_cpu_handoff(st, "other", "x", "2026-11-02T10:00:00Z", "other project session")
    add_cpu_handoff(st, "../evil/name", "y", "2026-11-03T10:00:00Z", "odd project session")
    nid = st.save_note("app", "Done: agent wrote this", "handoff", session_id="mid", agent="claude-code")
    with st.con:
        st.con.execute("UPDATE entries SET ts = '2026-11-07T10:00:00Z' WHERE id = ?", (nid,))
    st.save_note("app", "Use SQLite", "decision", agent="claude-code")
    st.save_note("research:ml", "A research fact", "fact")
    st.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    st.close()


def test_export_sessions_newest_first_agent_preferred_and_masked(tmp_path):
    db = tmp_path / "lore.db"
    seed(db)
    st = Store(db)
    md = st.export("app")
    st.close()
    sessions = md.split("## Sessions\n", 1)[1].splitlines()
    assert sessions[0] == "- 2026-11-09 [claude] newest cpu"
    assert sessions[1] == "- 2026-11-07 [claude-code] Done: agent wrote this"
    assert sessions[2].startswith("- 2026-11-01 [codex] İstek: old cpu Yapılan: used [REDACTED]")
    assert len(sessions) == 3 and "cpu text replaced" not in md and SECRET not in md
    assert "other project session" not in md and "## Decisions" in md


def test_export_all_writes_one_safe_file_per_project_and_keeps_db_unchanged(tmp_path):
    db = tmp_path / "data" / "lore.db"
    db.parent.mkdir()
    seed(db)
    before = db_digest(db)
    out = tmp_path / "book" / "nested"
    run_cli(["export", "--all", "-o", str(out)], db, tmp_path)
    files = sorted(p.name for p in out.iterdir())
    assert len(files) == 4 and "app.md" in files and "other.md" in files
    assert all(p.parent == out for p in out.rglob("*.md")) and not (tmp_path / "book" / "evil").exists()
    assert any(f.startswith(".._evil_name-") for f in files) and any(f.startswith("research_ml-") for f in files)
    assert "## Sessions" in (out / "app.md").read_text(encoding="utf-8")
    assert SECRET not in "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
    assert db_digest(db) == before


def test_export_all_merges_aliases_into_one_file(tmp_path):
    cfg = tmp_path / ".config" / "mesa-lore"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text('[aliases]\nold-app = "app"\n', encoding="utf-8")
    db = tmp_path / "lore.db"
    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        st = Store(db)
        add_cpu_handoff(st, "old-app", "s1", "2026-11-01T10:00:00Z", "alias session")
        add_cpu_handoff(st, "app", "s2", "2026-11-02T10:00:00Z", "canonical session")
        st.close()
    run_cli(["export", "--all", "-o", str(tmp_path / "book")], db, tmp_path)
    assert [p.name for p in (tmp_path / "book").iterdir()] == ["app.md"]
    text = (tmp_path / "book" / "app.md").read_text(encoding="utf-8")
    assert "alias session" in text and "canonical session" in text


def test_export_missing_db_is_not_created_and_all_needs_folder(tmp_path, capsys):
    db = tmp_path / "missing" / "lore.db"
    with pytest.raises(SystemExit) as e:
        run_cli(["export", "--project", "app"], db, tmp_path)
    assert "cannot read" in str(e.value) and not db.parent.exists()
    for bad in (["export", "--all"], ["export", "--all", "-o", "x", "--project", "app"]):
        with pytest.raises(SystemExit):
            run_cli(bad, db, tmp_path)
    assert "--all needs" in capsys.readouterr().err


def test_export_old_schema_is_not_migrated(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE entries (id TEXT, project TEXT, text TEXT, kind TEXT, state TEXT, agent TEXT, ts TEXT, source_type TEXT, session_id TEXT)")
    con.commit()
    con.close()
    before = db.read_bytes()
    with pytest.raises(SystemExit) as e:
        run_cli(["export", "--project", "app"], db, tmp_path)
    assert "doctor" in str(e.value) and db.read_bytes() == before
    assert json.dumps(str(e.value))
