import asyncio
import json
import math
import shutil
from pathlib import Path

from mesa_lore.mcp_server import TOOL_SPECS, app
from mesa_lore.store import Store

FIX = Path(__file__).resolve().parent.parent / "bench" / "fixtures"


def test_advertised_tool_schemas_are_tool_specs():
    # The startup budget is measured on TOOL_SPECS, so clients must be shown exactly that,
    # not the larger schemas the SDK generates from the function signatures.
    tools = asyncio.run(app.list_tools())
    shown = [{"name": t.name, "description": t.description, "inputSchema": t.input_schema} for t in tools]
    assert all(t.output_schema is None for t in tools)
    assert sorted(shown, key=lambda s: s["name"]) == sorted(TOOL_SPECS, key=lambda s: s["name"])
    assert math.ceil(len(json.dumps(shown, ensure_ascii=False)) / 4) <= 400


def test_existing_sessions_get_handoff_on_reingest(tmp_path):
    # A Round 3 DB already holds the chunks, so re-ingesting adds no chunks; it must still build handoffs.
    db = tmp_path / "r3.db"
    shutil.copyfile(FIX / "r3_schema.db", db)
    st = Store(db)
    try:
        assert st.con.execute("SELECT count(*) FROM handoffs").fetchone()[0] == 0
        sess = st.con.execute("SELECT DISTINCT session_id FROM entries WHERE session_id = 'a1'").fetchall()
        assert sess, "fixture DB should hold session a1"
        st.ingest_file(FIX / "claude" / "a1.jsonl", "claude")
        ho = st.con.execute("SELECT text FROM handoffs WHERE session_id = 'a1'").fetchall()
        assert len(ho) == 1 and "PostgreSQL" in ho[0][0]
    finally:
        st.close()


def _line(role, content, i):
    return json.dumps({"type": role, "sessionId": "hf1", "cwd": "/repo/app", "timestamp": f"2026-11-10T10:00:{i:02d}Z",
                       "message": {"role": role, "content": content}})


def test_handoff_files_are_changed_files_latest_first_kept(tmp_path):
    lines = [_line("user", "refactor the payment module", 0)]
    long = "src/payments/" + "x" * 30  # long paths: the 200-char line cap must drop the oldest, not the newest
    calls = [("Read", "only_read.py")] + [("Edit", f"{long}/changed_{k}.py") for k in range(8)] + [("Write", "new_file.py")]
    for k, (name, fp) in enumerate(calls):
        lines.append(_line("assistant", [{"type": "tool_use", "id": f"t{k}", "name": name, "input": {"file_path": fp}}], 2 * k + 1))
        lines.append(_line("user", [{"type": "tool_result", "tool_use_id": f"t{k}", "content": "ok"}], 2 * k + 2))
    lines.append(_line("assistant", "done", 59))
    p = tmp_path / "hf1.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(p, "claude")
        text = st.con.execute("SELECT text FROM handoffs WHERE session_id = 'hf1'").fetchone()[0]
        files_line = next(ln for ln in text.splitlines() if ln.startswith("Dosyalar:"))
        assert "only_read.py" not in files_line
        assert "new_file.py" in files_line and "changed_7.py" in files_line
    finally:
        st.close()


def test_handoff_and_index_title_skip_markup_tags(tmp_path):
    # 20 of 54 real handoffs started with a wrapper tag, so the 25-char index title showed only the tag.
    lines = [json.dumps({"type": "user", "sessionId": "tg1", "cwd": "/repo/app", "timestamp": "2026-11-10T10:00:00Z",
                         "message": {"role": "user", "content": '<pasted_content id="ab12">\nRole: Checker. Review the round.\n</pasted_content>'}}),
             _line("assistant", "Reviewed.", 1).replace('"hf1"', '"tg1"')]
    p = tmp_path / "tg1.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(p, "claude")
        text = st.con.execute("SELECT text FROM handoffs WHERE session_id = 'tg1'").fetchone()[0]
        assert text.startswith("İstek: Role: Checker. Review the round.")
        assert "Role: Checker" in st.index("app") and "<pasted" not in st.index("app")
    finally:
        st.close()


def _session(sid, user_text, i0=0):
    return [json.dumps({"type": "user", "sessionId": sid, "cwd": "/repo/app", "timestamp": f"2026-11-11T10:{i0:02d}:00Z",
                        "message": {"role": "user", "content": user_text}}),
            json.dumps({"type": "assistant", "sessionId": sid, "cwd": "/repo/app", "timestamp": f"2026-11-11T10:{i0:02d}:30Z",
                        "message": {"id": f"m_{sid}", "role": "assistant", "content": "ok",
                                    "usage": {"input_tokens": 100, "output_tokens": 5}}})]


def test_handoff_used_needs_handoff_lines_not_just_the_index(tmp_path):
    # Any SessionStart index starts with "Scopes:"; only an index that carried handoff lines opens a session "with handoff".
    from mesa_lore.text import wrap_untrusted
    st = Store(tmp_path / "lore.db")
    try:
        assert "Handoffs:" not in st.index("app")
        prev = tmp_path / "prev.jsonl"
        prev.write_text("\n".join(_session("prev", "set up the payment module")) + "\n", encoding="utf-8")
        st.ingest_file(prev, "claude")
        idx = st.index("app")
        assert "Handoffs:" in idx and "set up the payment" in idx
        quoted = wrap_untrusted('keys = ("Scopes:", "Handoffs:")')  # a dev session quoting the code is not a handoff start
        for sid, ctx, i0 in (("with_ho", wrap_untrusted(idx), 1), ("idx_only", wrap_untrusted("Scopes: app (1)"), 2),
                             ("quoted", quoted, 3)):
            p = tmp_path / f"{sid}.jsonl"
            p.write_text("\n".join(_session(sid, f"{ctx}\ncontinue", i0)) + "\n", encoding="utf-8")
            st.ingest_file(p, "claude")
        att = tmp_path / "attach.jsonl"  # hook context stored outside any message: only the raw-line scan sees it
        att.write_text("\n".join([json.dumps({"type": "attachment", "sessionId": "attach", "content": wrap_untrusted(idx)})]
                                 + _session("attach", "continue", 4)) + "\n", encoding="utf-8")
        st.ingest_file(att, "claude")
        used = dict(st.con.execute("SELECT session_id, MAX(handoff_used) FROM usage GROUP BY session_id").fetchall())
        mem = dict(st.con.execute("SELECT session_id, MAX(memory_used) FROM usage GROUP BY session_id").fetchall())
        assert used == {"prev": 0, "with_ho": 1, "idx_only": 0, "quoted": 0, "attach": 1}
        assert mem == {"prev": 0, "with_ho": 1, "idx_only": 1, "quoted": 1, "attach": 1}
    finally:
        st.close()


def test_uninstall_removes_only_mesa_lore_entries_and_keeps_later_changes(tmp_path, monkeypatch):
    from mesa_lore.install import run_install, run_uninstall
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANTIGRAVITY_BRAIN_DIR", str(tmp_path / "no-brain"))
    settings, claude_json = home / ".claude" / "settings.json", home / ".claude.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "sonnet", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
    claude_json.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    toml = home / ".codex" / "config.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text('model = "o5"\n')
    st = Store(tmp_path / "lore.db")
    try:
        run_install(st, home)
    finally:
        st.close()
    # Changes made after install must survive uninstall.
    s = json.loads(settings.read_text())
    s["theme"] = "light"
    settings.write_text(json.dumps(s))
    c = json.loads(claude_json.read_text())
    c["mcpServers"]["later"] = {"command": "y"}
    c["numStartups"] = 7
    claude_json.write_text(json.dumps(c))
    toml.write_text(toml.read_text() + '\n[mcp_servers.later]\ncommand = "z"\n')

    run_uninstall(home)
    s, c, t = json.loads(settings.read_text()), json.loads(claude_json.read_text()), toml.read_text()
    assert s["theme"] == "light" and s["model"] == "sonnet"
    assert s["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}
    assert c == {"mcpServers": {"other": {"command": "x"}, "later": {"command": "y"}}, "numStartups": 7}
    assert "mesa-lore" not in t and 'model = "o5"' in t and "[mcp_servers.later]" in t
    # Files that only install created are gone; backups stay for manual restore.
    assert not (home / ".codex" / "hooks.json").exists()
    assert not (home / ".gemini" / "antigravity" / "mcp_config.json").exists()
    assert (home / ".config" / "mesa-lore" / "backup" / "manifest.json").exists()


def test_index_counts_research_scopes_without_names(tmp_path):
    st = Store(tmp_path / "lore.db")
    try:
        st.save_note("app", "Use SQLite", "decision")
        for k in range(3):
            st.save_note(f"research:topic{k}", "secret research", "fact")
        idx = st.index("app")
        assert "+3 research scopes" in idx.splitlines()[0] and "research:" not in idx
        assert "research:topic1" in st.index("research:topic1")  # asked directly: its own name is shown
    finally:
        st.close()
