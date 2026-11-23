import ast
import asyncio
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import mesa_lore
from mesa_lore.readers.antigravity import parse_antigravity
from mesa_lore.store import Store


def _parse_antigravity(path):  # raw reader tuples (moved here from store.py: test-only helper)
    chunks, usages = parse_antigravity(path)
    return [(c["session_id"], c["cwd"], c["ts"], c["role"], c["text"], c["dedupe_key"], c["model"]) for c in chunks], usages


def test_annotations_resolve_where_defined():
    # Python < 3.14 evaluates annotations when the def runs (pyproject allows >= 3.10),
    # so a name used in an annotation must already exist at that point in the module.
    for path in sorted(Path(mesa_lore.__file__).parent.glob("*.py")):
        ns = {"__name__": f"eager_{path.stem}"}
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            funcs = [node] if isinstance(node, ast.FunctionDef) else []
            if isinstance(node, ast.ClassDef):
                funcs = [n for n in node.body if isinstance(n, ast.FunctionDef)]
            for fn in funcs:
                a = fn.args
                for ann in [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs] + [fn.returns]:
                    if ann is not None:
                        eval(compile(ast.Expression(ann), str(path), "eval"), ns)
            exec(compile(ast.Module([node], []), str(path), "exec"), ns)


def test_mcp_advertises_tool_spec_descriptions():
    from mesa_lore.mcp_server import TOOL_SPECS, app
    advertised = {t.name: t.description for t in asyncio.run(app.list_tools())}
    assert advertised == {s["name"]: s["description"] for s in TOOL_SPECS}


def _antigravity(tmp_path, steps, uris=None):
    logs = tmp_path / "brain" / "conv-c3" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    if uris:
        con = sqlite3.connect(tmp_path / "conversation_summaries.db")
        con.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, workspace_uris TEXT, project_id TEXT)")
        con.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", ("conv-c3", json.dumps(uris), "pid"))
        con.commit()
        con.close()
    tr = logs / "transcript.jsonl"
    tr.write_text("\n".join(json.dumps(s) for s in steps), encoding="utf-8")
    return tr


def _user(i, text):
    return {"step_index": i, "type": "USER_INPUT", "created_at": f"2026-11-08T10:00:0{i}Z", "content": text}


def _setting(old, new):
    return (f"<USER_SETTINGS_CHANGE>The user changed setting `Model Selection` from {old} to {new}. "
            "No need to comment on this change unless asked.</USER_SETTINGS_CHANGE>")


def test_antigravity_model_keeps_dots_and_follows_changes(tmp_path):
    tr = _antigravity(tmp_path, [
        _user(0, _setting("None", "Gemini 3.8 Flash (High)") + "<USER_REQUEST>ilk soru</USER_REQUEST>"),
        _user(1, _setting("Gemini 3.8 Flash (High)", "Gemini 4 Argon (Medium)") + "<USER_REQUEST>ikinci soru</USER_REQUEST>"),
    ])
    with patch.dict(os.environ, {"ANTIGRAVITY_BRAIN_DIR": str(tmp_path / "brain")}):
        res = _parse_antigravity(tr)
        chunks = res[0] if isinstance(res, tuple) else res
    assert [(c[4], c[6]) for c in chunks] == [("ilk soru", "Gemini 3.8 Flash (High)"),
                                               ("ikinci soru", "Gemini 4 Argon (Medium)")]


def test_antigravity_workspace_uri_is_percent_decoded(tmp_path):
    tr = _antigravity(tmp_path, [_user(0, "Tez teslim tarihi belirlendi.")],
                      uris=["file:///home/u/Masa%C3%BCst%C3%BC/Y%C3%BCksek%20Lisans"])
    st = Store(tmp_path / "t.db")
    try:
        with patch.dict(os.environ, {"ANTIGRAVITY_BRAIN_DIR": str(tmp_path / "brain")}):
            assert st.ingest_file(tr, agent="antigravity") == 1
        assert st.search("tez teslim tarihi", "Yüksek Lisans")["status"] == "ok"
    finally:
        st.close()


def test_kind_filter_also_applies_to_replace_partner(tmp_path):
    st = Store(tmp_path / "t.db")
    try:
        old = st.save_note("p", "Kuyruk için RabbitMQ kullanılacak.", "todo")
        new = st.save_note("p", "Kuyruk için RabbitMQ yerine Redis Streams seçildi.", "decision", replaces=old)
        r = st.search("kuyruk RabbitMQ", "p", kind="decision")
        assert [x["id"] for x in r["results"]] == [new]
        r_all = st.search("kuyruk RabbitMQ", "p")
        assert {x["id"] for x in r_all["results"]} == {old, new}
    finally:
        st.close()

