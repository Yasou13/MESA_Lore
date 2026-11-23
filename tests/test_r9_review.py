import json
import subprocess

from mesa_lore.readers import tool_chunk
from mesa_lore.store import Store


def _repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _claude(path, cwd, sid, turns):
    path.write_text("\n".join(json.dumps({"type": role, "sessionId": sid, "cwd": cwd, "timestamp": f"2026-11-23T10:00:{i:02d}Z",
                                           "message": {"role": role, "content": content}}) for i, (role, content) in enumerate(turns)), encoding="utf-8")
    return path


def _tool(name, tid, args):
    return [("assistant", [{"type": "tool_use", "id": tid, "name": name, "input": args}]),
            ("user", [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}])]


def test_secret_cut_by_handoff_clip_is_fully_masked(tmp_path):
    repo = _repo(tmp_path / "app")
    source = _claude(tmp_path / "s.jsonl", str(repo), "s", [("user", "a" * 84 + " AKIAIOSFODNN7EXAMPLE tail")])
    st = Store(tmp_path / "l.db")
    try:
        st.ingest_file(source, "claude")
        assert "AKIA" not in st.con.execute("SELECT text FROM handoffs").fetchone()[0]
    finally:
        st.close()


def test_invalid_path_escape_does_not_abort_ingest(tmp_path):
    args = '*** Begin Patch\n*** Update File: conf.json\n+  "path": "C:\\\\Users\\\\me"'.replace("\\\\", "\\")
    assert "conf.json" in tool_chunk("s", "/x", None, "p", None, "apply_patch", args, "ok")["paths"]


def test_null_cwd_and_edited_repo_outranks_read_repo(tmp_path):
    read, edited, plain = _repo(tmp_path / "read"), _repo(tmp_path / "edited"), tmp_path / "plain"
    plain.mkdir()
    st = Store(tmp_path / "l.db")
    try:
        st.ingest_file(_claude(tmp_path / "n.jsonl", None, "n", [("user", "x"), *_tool("Edit", "e", {"file_path": "a.py"})]), "claude")
        st.ingest_file(_claude(tmp_path / "r.jsonl", str(plain), "r", [
            ("user", "x"), *_tool("Read", "a", {"file_path": str(read / "a.md")}), *_tool("Edit", "b", {"file_path": str(edited / "b.py")})]), "claude")
        assert st.con.execute("SELECT project FROM handoffs WHERE session_id = 'n'").fetchone()[0] == "misc"
        assert st.con.execute("SELECT project FROM handoffs WHERE session_id = 'r'").fetchone()[0] == "edited"
    finally:
        st.close()


def test_real_edit_tools_and_json_encoded_patches_count_as_changes():
    for name in ("MultiEdit", "NotebookEdit", "write_to_file", "replace_file_content", "multi_replace_file_content"):
        assert tool_chunk("s", "/x", None, "p", None, name, json.dumps({"file_path": "a.py"}), "ok")["changed"]
    args = json.dumps({"input": "*** Begin Patch\n*** Update File: src/a.py\n@@\n-a\n+b"})
    assert tool_chunk("s", "/x", None, "p", None, "apply_patch", args, "ok")["paths"] == ["src/a.py"]


def test_printed_log_text_is_not_a_tool_error():
    for output in ("Traceback (most recent call last):\nValueError: old log", "one\ntwo\nthree\nfour\nExit code: 1"):
        assert "error" not in tool_chunk("s", "/x", None, "p", None, "Bash", json.dumps({"command": "cat log"}), output)
    assert "error" in tool_chunk("s", "/x", None, "p", None, "Bash", "{}", "Exit code: 2\nWall time: 1s")


def test_session_moving_into_excluded_project_leaves_no_rows(tmp_path):
    cfg = tmp_path / "isolated/home/.config/mesa-lore/config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('excluded_projects = ["private"]\n', encoding="utf-8")
    repo, plain = _repo(tmp_path / "private"), tmp_path / "plain"
    plain.mkdir()
    source = _claude(tmp_path / "s.jsonl", str(plain), "s", [("user", "PRIVATE")])
    st = Store(tmp_path / "l.db")
    try:
        st.ingest_file(source, "claude")
        _claude(source, str(plain), "s", [("user", "PRIVATE"), *_tool("Edit", "e", {"file_path": str(repo / "k.py")})])
        st.ingest_file(source, "claude")
        assert st.con.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
    finally:
        st.close()
