from mesa_lore.install import run_doctor
from mesa_lore.store import Store


def test_doctor_drops_warnings_for_deleted_files_only(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\nstill not json\n", encoding="utf-8")
    gone = tmp_path / "gone.jsonl"
    gone.write_text("not json\nstill not json\n", encoding="utf-8")
    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(bad, "claude")
        st.ingest_file(gone, "claude")
        assert len(st.warnings()) == 2
        gone.unlink()
        out = run_doctor(st, tmp_path)
        assert str(bad) in out and str(gone) not in out
        assert [w["path"] for w in st.warnings()] == [str(bad)]
    finally:
        st.close()


def test_doctor_keeps_warning_for_unreadable_file(tmp_path, monkeypatch):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\nstill not json\n", encoding="utf-8")
    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(bad, "claude")

        def denied(*_a, **_k):
            raise PermissionError("denied")
        monkeypatch.setattr("mesa_lore.install.parse_transcript", denied)
        assert str(bad) in run_doctor(st, tmp_path)
        assert len(st.warnings()) == 1
    finally:
        st.close()
