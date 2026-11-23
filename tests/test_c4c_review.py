import json
import sqlite3
import subprocess

from mesa_lore import store as store_mod
from mesa_lore.store import MIGRATIONS, Store, get_project


def _v5_db(path, handoff_text):
    con = sqlite3.connect(path)
    with con:
        for ver, step in MIGRATIONS[:5]:
            if isinstance(step, str):
                for s in step.split(";"):
                    if s.strip():
                        con.execute(s)
            else:
                step(con)
            con.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, 'x')", (ver,))
        con.execute("INSERT INTO handoffs (id, project, text, session_id, ts, state, agent) "
                    "VALUES ('h1', 'p', ?, 's1', '2026-01-01T00:00:00Z', 'active', 'claude')", (handoff_text,))
    con.close()


def test_v5_to_v7_migration_folds_handoff_fts_once(tmp_path):
    # v6 indexed handoffs already folded, then v7 folded every row again: a Turkish handoff got 3 folded copies.
    _v5_db(tmp_path / "v5.db", "İstek: şifre değişikliği yapıldı")
    st = Store(tmp_path / "v5.db")
    try:
        txt = st.con.execute("SELECT text_norm FROM entries_fts WHERE id = 'h1'").fetchone()[0]
        assert txt.count("sifre degisikligi") == 1 and txt.count("şifre değişikliği") == 1, txt
    finally:
        st.close()


def test_ingest_resolves_project_once_per_cwd(tmp_path, monkeypatch):
    # get_project runs `git rev-parse`; it must not run once per transcript line.
    calls = []
    real = store_mod.get_project
    monkeypatch.setattr(store_mod, "get_project", lambda cwd: calls.append(cwd) or real(cwd))
    tf = tmp_path / "s.jsonl"
    tf.write_text("\n".join(json.dumps({
        "type": "user", "sessionId": "s1", "cwd": str(tmp_path), "timestamp": f"2026-11-10T10:00:{i:02d}Z",
        "message": {"role": "user", "content": f"message number {i} about parquet"}}) for i in range(20)) + "\n", encoding="utf-8")
    st = Store(tmp_path / "lore.db")
    try:
        assert st.ingest_file(tf, "claude") > 0
        assert len(calls) == 1, len(calls)
    finally:
        st.close()


def test_project_identity_ignores_inherited_git_env(tmp_path, monkeypatch):
    # A GIT_DIR inherited from the caller (e.g. ingest run from a git hook) must not make every repo resolve
    # to that one repo's name: that would merge unrelated projects.
    for name in ("repo-a", "repo-b"):
        (tmp_path / name).mkdir()
        subprocess.run(["git", "init", "-q"], cwd=tmp_path / name, check=True)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "repo-a" / ".git"))
    assert get_project(str(tmp_path / "repo-b")) == "repo-b"
