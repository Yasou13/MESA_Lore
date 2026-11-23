import json
from pathlib import Path
from mesa_lore.install import run_doctor
from mesa_lore.store import Store

META = {"timestamp": "2026-11-21T01:00:00Z", "type": "session_meta", "payload": {"id": "s1", "cwd": "/repo/app", "cli_version": "1.0"}}


def _write(path, *lines):
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return path


def test_metadata_only_codex_rollout_is_not_a_warning(tmp_path):
    f = _write(tmp_path / "rollout-meta.jsonl", META)
    st = Store(tmp_path / "lore.db")
    try:
        assert st.ingest_file(f, "codex") == 0
        assert st.warnings() == []
    finally:
        st.close()


def test_unrecognized_or_corrupt_codex_files_still_warn(tmp_path):
    other = _write(tmp_path / "rollout-other.jsonl", {"type": "something_new", "payload": {"x": 1}})
    mixed = _write(tmp_path / "rollout-mixed.jsonl", META, {"type": "something_new", "payload": {"x": 1}})
    corrupt = tmp_path / "rollout-bad.jsonl"
    corrupt.write_text(json.dumps(META) + "\n{broken\n{broken\n", encoding="utf-8")
    st = Store(tmp_path / "lore.db")
    try:
        for f in (other, mixed, corrupt):
            st.ingest_file(f, "codex")
        assert {Path(w["path"]).name for w in st.warnings()} == {other.name, mixed.name, corrupt.name}
    finally:
        st.close()


def test_doctor_drops_old_metadata_warning_but_keeps_row_and_real_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("MESA_LORE_DB", str(tmp_path / "lore.db"))
    meta = _write(tmp_path / "rollout-meta.jsonl", META)
    real = _write(tmp_path / "rollout-real.jsonl", {"type": "something_new", "payload": {"x": 1}})
    gone = tmp_path / "rollout-gone.jsonl"
    st = Store(tmp_path / "lore.db")
    try:
        for p in (meta, real, gone):  # rows saved by the older reader
            st.con.execute("INSERT INTO ingest_warnings VALUES (?, 'codex', 'unrecognized format: 0 chunks from 1 lines', '2026-11-21T01:00:00Z')", (str(p),))
        st.con.commit()
        out = run_doctor(st, tmp_path)
        assert "Warnings (1):" in out and real.name in out and gone.name not in out and meta.name not in out
        assert {w["path"] for w in st.warnings()} == {str(meta), str(real)}  # R7: only the deleted file's row is dropped
    finally:
        st.close()
