from datetime import datetime, timedelta, timezone
import json
import os
import sys
from io import StringIO
from unittest.mock import patch

from mesa_lore.cli import get_queue_path, main
from mesa_lore.store import Store, _load_excluded_projects


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_purge_older_than_90d(tmp_path):
    db = tmp_path / "lore.db"
    old_ts = _iso(120)
    new_ts = _iso(10)

    old_tr = tmp_path / "old.jsonl"
    old_tr.write_text("\n".join([
        json.dumps({"type": "user", "sessionId": "s_old", "cwd": "/repo/proj-a", "timestamp": old_ts,
                    "message": {"role": "user", "content": "ancient legacy token config"}}),
        json.dumps({"type": "assistant", "sessionId": "s_old", "cwd": "/repo/proj-a", "timestamp": old_ts,
                    "message": {"id": "m_old", "role": "assistant", "content": "ok ancient",
                                "usage": {"input_tokens": 100, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 5}}}),
    ]) + "\n", encoding="utf-8")

    new_tr = tmp_path / "new.jsonl"
    new_tr.write_text("\n".join([
        json.dumps({"type": "user", "sessionId": "s_new", "cwd": "/repo/proj-a", "timestamp": new_ts,
                    "message": {"role": "user", "content": "recent fresh deployment config"}}),
        json.dumps({"type": "assistant", "sessionId": "s_new", "cwd": "/repo/proj-a", "timestamp": new_ts,
                    "message": {"id": "m_new", "role": "assistant", "content": "ok recent",
                                "usage": {"input_tokens": 200, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 5}}}),
    ]) + "\n", encoding="utf-8")

    other_tr = tmp_path / "other.jsonl"
    other_tr.write_text(json.dumps({
        "type": "user", "sessionId": "s_oth", "cwd": "/repo/proj-b", "timestamp": old_ts,
        "message": {"role": "user", "content": "ancient proj-b keep"},
    }) + "\n", encoding="utf-8")

    st = Store(db)
    try:
        st.ingest_file(old_tr, "claude")
        st.ingest_file(new_tr, "claude")
        st.ingest_file(other_tr, "claude")
        nid_old = st.save_note("proj-a", "ancient note", "fact")
        st.con.execute("UPDATE entries SET ts = ? WHERE id = ?", (old_ts, nid_old))
        st.con.commit()
    finally:
        st.close()

    # Scoped purge --older-than 90d --project proj-a leaves proj-b old entry intact
    buf = StringIO()
    with patch.dict(os.environ, {"MESA_LORE_DB": str(db)}):
        with patch.object(sys, "argv", ["mesa-lore", "purge", "--older-than", "90d", "--project", "proj-a"]):
            with patch("sys.stdout", buf):
                main()
    assert "Purged" in buf.getvalue()

    st = Store(db)
    try:
        assert st.search("ancient", "proj-a", budget_tokens=300)["status"] == "no_match"
        assert st.search("fresh", "proj-a", budget_tokens=300)["status"] == "ok"
        assert st.get(nid_old) is None
        assert st.con.execute("SELECT count(*) FROM entries WHERE project = 'proj-a'").fetchone()[0] == 2
        assert st.con.execute("SELECT count(*) FROM handoffs WHERE project = 'proj-a'").fetchone()[0] == 1
        assert st.con.execute("SELECT count(*) FROM usage WHERE project = 'proj-a'").fetchone()[0] == 1
        assert st.search("ancient", "proj-b", budget_tokens=300)["status"] == "ok"
    finally:
        st.close()

    # Global purge --older-than 90d clears proj-b old entry too
    with patch.dict(os.environ, {"MESA_LORE_DB": str(db)}):
        with patch.object(sys, "argv", ["mesa-lore", "purge", "--older-than", "90d"]):
            main()
    st = Store(db)
    try:
        assert st.search("ancient", "proj-b", budget_tokens=300)["status"] == "no_match"
        assert st.search("fresh", "proj-a", budget_tokens=300)["status"] == "ok"
    finally:
        st.close()


def test_excluded_projects_in_config_toml_and_py310_fallback(tmp_path):
    home = tmp_path / "home"
    cfg = home / ".config" / "mesa-lore" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('# privacy config\nexcluded_projects = ["secret-proj", "private-tmp"]\n', encoding="utf-8")

    # Verify fallback parser works even when stdlib tomllib is unavailable (Python 3.10)
    with patch.dict(sys.modules, {"tomllib": None}):
        assert _load_excluded_projects(home) == {"secret-proj", "private-tmp"}

    db = tmp_path / "lore.db"
    sec_cl = tmp_path / "sec_cl.jsonl"
    sec_cl.write_text(json.dumps({
        "type": "user", "sessionId": "sec1", "cwd": "/repo/secret-proj", "timestamp": _iso(1),
        "message": {"role": "user", "content": "confidential secret payload"},
    }) + "\n", encoding="utf-8")
    pub_cl = tmp_path / "pub_cl.jsonl"
    pub_cl.write_text(json.dumps({
        "type": "user", "sessionId": "pub1", "cwd": "/repo/public-proj", "timestamp": _iso(1),
        "message": {"role": "user", "content": "public docs payload"},
    }) + "\n", encoding="utf-8")

    ag_sec = home / ".gemini" / "antigravity" / "brain" / "ag-sec" / ".system_generated" / "logs" / "transcript.jsonl"
    ag_sec.parent.mkdir(parents=True)
    ag_sec.write_text(json.dumps({
        "type": "USER_INPUT", "step_index": 0, "created_at": _iso(1),
        "content": "<user_information>\nActive Workspaces:\n- /repo/private-tmp\n</user_information>\nhidden ag work",
    }) + "\n", encoding="utf-8")

    q = tmp_path / "queue.txt"
    q.write_text(f"{sec_cl}\n{pub_cl}\n", encoding="utf-8")

    with patch.dict(os.environ, {
        "HOME": str(home), "MESA_LORE_DB": str(db), "MESA_LORE_QUEUE": str(q),
        "ANTIGRAVITY_BRAIN_DIR": str(home / ".gemini" / "antigravity" / "brain"),
    }):
        assert str(get_queue_path()) == str(q)
        with patch.object(sys, "argv", ["mesa-lore", "ingest-pending", "--home", str(home)]):
            main()

    st = Store(db)
    try:
        assert st.list_entries("secret-proj") == []
        assert st.list_entries("private-tmp") == []
        assert st.search("public", "public-proj", budget_tokens=300)["status"] == "ok"
        assert st.warnings() == []
    finally:
        st.close()
