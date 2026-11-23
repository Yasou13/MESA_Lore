import os
import sys
from unittest.mock import patch
from mesa_lore.text import tr_stem, trim_tool_output
from mesa_lore.store import Store
from mesa_lore.cli import main


def run_cli(args, env):
    with patch.dict(os.environ, env):
        with patch.object(sys, "argv", ["mesa-lore"] + args):
            main()


def test_consonant_softening_both_ways():
    # p <-> b
    assert "kitab" in tr_stem("kitap")
    assert "kitap" in tr_stem("kitabı")
    assert "dolab" in tr_stem("dolap")
    assert "dolap" in tr_stem("dolabı")

    # t <-> d
    assert "periyod" in tr_stem("periyot")
    assert "periyot" in tr_stem("periyodu")
    assert "kağit" in tr_stem("kağıdı")
    assert "kağid" in tr_stem("kağıt")

    # k <-> ğ
    assert "değişiklik" in tr_stem("değişikliği")
    assert "değişikliğ" in tr_stem("değişiklik")

    # ç <-> c
    assert "ağac" in tr_stem("ağaç")
    assert "ağaç" in tr_stem("ağacı")


def test_trim_tool_input():
    long_input = "prefix_start " + ("MIDDLE_KEYWORD_TO_TRIM " * 200) + " suffix_end"
    trimmed = trim_tool_output(long_input)
    assert len(trimmed) <= 2010
    assert "prefix_start" in trimmed
    assert "suffix_end" in trimmed
    assert "\n...\n" in trimmed


def test_cli_show_defaults_to_cwd_project(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "test.db"
    env = {"MESA_LORE_DB": str(db_path)}
    st = Store(db_path)
    try:
        n_other = st.save_note("other-project", "Secret info in other project.", "decision")
    finally:
        st.close()

    # In current working directory (not other-project), show should not find it
    run_cli(["show", n_other], env)
    captured = capsys.readouterr().out
    assert f"Not found: {n_other}" in captured
