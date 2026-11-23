import math
from mesa_lore.store import Store


def test_index_content_and_isolation(tmp_path):
    db = tmp_path / "lore.db"
    st = Store(db)

    # Ingest sessions for two projects
    t1 = tmp_path / "projA.jsonl"
    t1.write_text(
        '{"type": "user", "sessionId": "sA", "cwd": "/repo/projA", "timestamp": "2026-03-01T12:00:00Z", "message": {"content": "Starting feature in projA"}}\n',
        encoding="utf-8"
    )
    t2 = tmp_path / "projB.jsonl"
    t2.write_text(
        '{"type": "user", "sessionId": "sB", "cwd": "/repo/projB", "timestamp": "2026-03-02T12:00:00Z", "message": {"content": "Starting feature in projB"}}\n',
        encoding="utf-8"
    )
    st.ingest_file(t1, agent="claude")
    st.ingest_file(t2, agent="claude")

    # Notes
    old_id = st.save_note("projA", "Old projA note", "decision")
    st.save_note("projA", "Active projA note", "decision", replaces=old_id)
    st.save_note("projB", "Secret projB note", "decision")
    st.save_note("global", "Global note for all", "fact")

    idx = st.index("projA", budget_tokens=400)
    assert "Active projA note" in idx
    assert "Global note for all" in idx
    assert "Old projA note" not in idx  # superseded
    assert "Secret projB note" not in idx  # isolated
    assert "Starting feature in projB" not in idx  # session B isolated
    assert "sB" not in idx
    assert "Starting feature in projA" in idx
    assert "2026-03-01" in idx
    assert math.ceil(len(idx) / 4) <= 400

    st.close()


def test_index_budget_limit(tmp_path):
    db = tmp_path / "lore.db"
    st = Store(db)

    for i in range(20):
        st.save_note("proj", f"Detailed note number {i} with lots of context and words.", "fact")

    budget = 50
    idx = st.index("proj", budget_tokens=budget)
    assert math.ceil(len(idx) / 4) <= budget
    assert len(idx) > 0

    # Budget smaller than any single note must return empty string (not a bare 'Notes:' header)
    tiny_idx = st.index("proj", budget_tokens=3)
    assert tiny_idx == ""
    assert math.ceil(len(tiny_idx) / 4) <= 3

    st.close()
