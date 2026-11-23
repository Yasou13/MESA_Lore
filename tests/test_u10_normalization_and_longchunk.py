import json
import math
from mesa_lore.text import tr_lower, normalize, trim_tool_output, split_text
from mesa_lore.store import Store


def test_normalization_folding():
    assert normalize("VERİTABANINI") == "veritabanini"
    assert normalize("veritabanı") == "veritabani"
    assert normalize("MIGRATION") == "migration"
    assert normalize("migration") == "migration"
    assert normalize("DAKİKADA") == "dakikada"
    assert normalize("sınırlar") == "sinirlar"
    assert tr_lower("IŞIK") == "ışık"
    assert tr_lower("İSTANBUL") == "istanbul"


def test_trim_tool_output():
    short_text = "hello world " * 10
    assert trim_tool_output(short_text) == short_text

    long_text = "START_" + ("A" * 1500) + "_MIDDLE_" + ("B" * 1500) + "_END"
    trimmed = trim_tool_output(long_text)
    assert len(trimmed) == 1200 + len("\n...\n") + 800
    assert trimmed.startswith("START_")
    assert trimmed.endswith("_END")
    assert "..." in trimmed


def test_split_text_paragraphs():
    p1 = "Paragraph one with some text. " * 10  # ~300 chars (~75 tokens)
    p2 = "Paragraph two with another text. " * 10
    p3 = "Paragraph three with more content. " * 10
    p4 = "Paragraph four concluding the note. " * 10

    full = f"{p1}\n\n{p2}\n\n{p3}\n\n{p4}"
    pieces = split_text(full, max_tokens=100)  # max 400 chars per piece
    assert len(pieces) > 1
    for piece in pieces:
        assert math.ceil(len(piece) / 4) <= 100
        assert piece.strip() == piece


def test_split_text_long_line():
    long_line = "word " * 300  # 1500 chars, no newlines
    pieces = split_text(long_line, max_tokens=100)
    assert len(pieces) > 1
    for piece in pieces:
        assert math.ceil(len(piece) / 4) <= 100


def test_ingest_splitting_and_dedupe(tmp_path):
    p1 = "First paragraph of document. " * 20  # ~600 chars (~150 tokens)
    p2 = "Second paragraph with information about delivery_eta in MIGRATION 0042. " * 15  # ~1000 chars (~250 tokens)
    p3 = "Third paragraph ending the text. " * 20

    long_text = f"{p1}\n\n{p2}\n\n{p3}"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "sessionId": "s1", "cwd": "/repo/proj", "timestamp": "2026-01-01", "message": {"content": long_text}}) + "\n",
        encoding="utf-8"
    )

    db = tmp_path / "lore.db"
    st = Store(db)
    inserted = st.ingest_file(transcript, agent="claude")
    assert inserted > 1  # Was split into multiple chunks

    # Re-ingest should be deduplicated
    assert st.ingest_file(transcript, agent="claude") == 0

    # Search for uppercase and lowercase terms
    r = st.search("MIGRATION 0042", "proj")
    assert r["status"] == "ok"
    assert any("0042" in x["text"] for x in r["results"])

    # Quote verification across split pieces must still set verified=True
    cross_quote = f"{p1.strip()[-40:]} {p2.strip()[:40]}"
    nid = st.save_note("proj", "Note with quote spanning two pieces", "fact", session_id="s1", quote=cross_quote)
    assert st.get(nid)["verified"] is True

    st.close()
