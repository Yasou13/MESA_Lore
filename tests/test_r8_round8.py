import json
from mesa_lore.store import Store
from mesa_lore.text import trim_tool_output


def test_item3_clean_user_prompts_slash_tags_and_over_600_chars(tmp_path):
    tf = tmp_path / "s1.jsonl"
    wrapped_prompt = (
        "<command-name>/review</command-name>\n"
        "<command-message>review</command-message>\n"
        "<command-args>--strict</command-args>\n"
        "<local-command-stdout>ok stdout</local-command-stdout>\n"
        "<local-command-stderr>warn stderr</local-command-stderr>\n"
        "<local-command-caveat>Caveat: local command output</local-command-caveat>\n"
        "Check the authentication middleware token rotation."
    )
    first_line = "Please review this very long pasted specification and configuration for the gateway service right now"
    body = "\n" + ("DETAILED_PASTED_BODY_ENTRY_9911 " * 30)  # well over 600 chars total
    long_prompt = first_line + body
    assert len(long_prompt) > 600

    lines = [
        {"type": "user", "sessionId": "s_clean", "cwd": "/repo/app", "timestamp": "2026-11-20T10:00:00Z",
         "message": {"role": "user", "content": wrapped_prompt}},
        {"type": "user", "sessionId": "s_clean", "cwd": "/repo/app", "timestamp": "2026-11-20T10:01:00Z",
         "message": {"role": "user", "content": long_prompt}},
    ]
    tf.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(tf, "claude")
        user_rows = [r["text"] for r in st.con.execute("SELECT text FROM entries WHERE source_type = 'user' ORDER BY ts").fetchall()]

        # 1) Slash-command wrapper tags dropped
        for tag in ("<command-name>", "</command-name>", "<command-message>", "<command-args>",
                    "<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>"):
            assert tag not in user_rows[0]
        assert "Check the authentication middleware token rotation." in user_rows[0]

        # 2) User message over 600 chars replaced with first line (max 80 chars) + "[pasted content, ~N chars]" in index and handoff
        ho = st.con.execute("SELECT text FROM handoffs WHERE session_id = 's_clean'").fetchone()[0]
        assert "[pasted content, ~" in ho
        assert "DETAILED_PASTED_BODY_ENTRY_9911" not in ho
        for tag in ("<command-name>", "<local-command-stdout>", "<local-command-caveat>"):
            assert tag not in ho
        idx = st.index("app")
        assert "DETAILED_PASTED_BODY_ENTRY_9911" not in idx
        assert "Please review this very long pasted specification and configuration for the gate" in ho
        # Full text remains searchable in entries
        assert st.search("DETAILED_PASTED_BODY_ENTRY_9911", "app")["status"] == "ok"
    finally:
        st.close()


def test_item4_record_once_dedup_by_normalized_text_hash_per_scope(tmp_path):
    f1 = tmp_path / "s1.jsonl"
    f2 = tmp_path / "s2.jsonl"
    f3 = tmp_path / "s3.jsonl"
    long_asst = "Run MIGRATION 0077 on the staging replica now. " + ("Detailed verification log entry for dedup scope check. " * 4)
    assert len(long_asst) >= 200
    # Same scope ("app"): two different sessions/turns with identical normalized text >= 200 chars (non-user)
    f1.write_text(json.dumps({
        "type": "assistant", "sessionId": "s1", "cwd": "/repo/app", "timestamp": "2026-11-20T10:00:00Z",
        "message": {"role": "assistant", "content": long_asst},
    }) + "\n", encoding="utf-8")
    f2.write_text(json.dumps({
        "type": "assistant", "sessionId": "s2", "cwd": "/repo/app", "timestamp": "2026-11-20T11:00:00Z",
        "message": {"role": "assistant", "content": long_asst.lower()},
    }) + "\n", encoding="utf-8")
    # Different scope ("other-app"): same text should NOT be skipped
    f3.write_text(json.dumps({
        "type": "assistant", "sessionId": "s3", "cwd": "/repo/other-app", "timestamp": "2026-11-20T12:00:00Z",
        "message": {"role": "assistant", "content": long_asst.lower()},
    }) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        assert st.ingest_file(f1, "claude") == 1
        assert st.ingest_file(f2, "claude") == 0
        assert st.ingest_file(f3, "claude") == 1
        app_cnt = st.con.execute("SELECT count(*) FROM entries WHERE project = 'app' AND source_type != 'note'").fetchone()[0]
        other_cnt = st.con.execute("SELECT count(*) FROM entries WHERE project = 'other-app' AND source_type != 'note'").fetchone()[0]
        assert app_cnt == 1
        assert other_cnt == 1
    finally:
        st.close()


def test_r8b_b_dedup_only_200_plus_chars_and_never_skip_user_messages(tmp_path):
    f1 = tmp_path / "b1.jsonl"
    f2 = tmp_path / "b2.jsonl"
    short_asst = "Short assistant reply under 200 chars."
    long_user = "User prompt over 200 chars that is repeated in another session. " * 4
    assert len(short_asst) < 200 and len(long_user) >= 200
    for fp, sid in ((f1, "sb1"), (f2, "sb2")):
        fp.write_text("\n".join([
            json.dumps({"type": "user", "sessionId": sid, "cwd": "/repo/app", "timestamp": "2026-11-20T10:00:00Z",
                        "message": {"role": "user", "content": long_user}}),
            json.dumps({"type": "assistant", "sessionId": sid, "cwd": "/repo/app", "timestamp": "2026-11-20T10:01:00Z",
                        "message": {"role": "assistant", "content": short_asst}}),
        ]) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        assert st.ingest_file(f1, "claude") == 2
        # Both the repeated >200-char user message and the repeated <200-char assistant message must be kept!
        assert st.ingest_file(f2, "claude") == 2
    finally:
        st.close()


def test_item5_cpu_handoff_per_session_signals_and_agent_handoff_wins(tmp_path):
    tf = tmp_path / "s_sig.jsonl"
    lines = [
        {"type": "user", "sessionId": "s_sig", "cwd": "/repo/app", "timestamp": "2026-11-20T10:00:00Z",
         "message": {"role": "user", "content": "<command-name>/fix</command-name> Fix the billing tax rounding bug."}},
        {"type": "assistant", "sessionId": "s_sig", "cwd": "/repo/app", "timestamp": "2026-11-20T10:01:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t_read", "name": "Read", "input": {"file_path": "/repo/app/src/tax_rules.py"}},
             {"type": "tool_use", "id": "t_edit", "name": "Edit", "input": {"file_path": "/repo/app/src/billing.py", "old_string": "a", "new_string": "b"}},
             {"type": "tool_use", "id": "t_bash", "name": "Bash", "input": {"command": "pytest tests/test_billing.py"}},
         ]}},
        {"type": "user", "sessionId": "s_sig", "cwd": "/repo/app", "timestamp": "2026-11-20T10:02:00Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t_read", "content": "TAX_RATE = 0.18"},
             {"type": "tool_result", "tool_use_id": "t_edit", "content": "Updated src/billing.py"},
             {"type": "tool_result", "tool_use_id": "t_bash", "content": "ValueError: negative tax amount in round_tax"},
         ]}},
        {"type": "assistant", "sessionId": "s_sig", "cwd": "/repo/app", "timestamp": "2026-11-20T10:03:00Z",
         "message": {"role": "assistant", "content": "Fixed round_tax in billing.py and verified tests."}},
    ]
    tf.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(tf, "claude")
        ho = st.con.execute("SELECT text FROM handoffs WHERE session_id = 's_sig'").fetchone()[0]
        ho_lines = ho.splitlines()
        # 1) What the user asked (cleaned)
        assert any(ln.startswith("İstek:") and "Fix the billing tax rounding bug." in ln and "<command-name>" not in ln for ln in ho_lines)
        # 2) Files modified
        assert any(ln.startswith("Dosyalar:") and "billing.py" in ln and "tax_rules.py" not in ln for ln in ho_lines)
        # 3) Files read (top few)
        assert any(ln.startswith("Okunan:") and "tax_rules.py" in ln for ln in ho_lines)
        # 4) Errors hit
        assert any(ln.startswith("Hatalar:") and "ValueError: negative tax amount" in ln for ln in ho_lines)
        # 5) Agent-written handoff wins when present
        st.save_note("app", "Agent handoff: billing tax rounding shipped cleanly.", kind="handoff", session_id="s_sig", agent="claude-code")
        idx = st.index("app")
        assert "billing tax rounding shipped cleanly" in idx
    finally:
        st.close()


def test_item6_startup_shows_previous_session_last_assistant_message_capped_450_tokens(tmp_path):
    import math
    from mesa_lore.mcp_server import TOOL_SPECS
    tf = tmp_path / "s_last.jsonl"
    lines = [
        {"type": "user", "sessionId": "s_old", "cwd": "/repo/app", "timestamp": "2026-11-19T10:00:00Z",
         "message": {"role": "user", "content": "Old session request"}},
        {"type": "assistant", "sessionId": "s_old", "cwd": "/repo/app", "timestamp": "2026-11-19T10:05:00Z",
         "message": {"role": "assistant", "content": "Old assistant reply that should not win"}},
        {"type": "user", "sessionId": "s_prev", "cwd": "/repo/app", "timestamp": "2026-11-20T10:00:00Z",
         "message": {"role": "user", "content": "Check final deployment status"}},
        {"type": "assistant", "sessionId": "s_prev", "cwd": "/repo/app", "timestamp": "2026-11-20T10:01:00Z",
         "message": {"role": "assistant", "content": "Intermediate assistant check"}},
        {"type": "assistant", "sessionId": "s_prev", "cwd": "/repo/app", "timestamp": "2026-11-20T10:05:00Z",
         "message": {"role": "assistant", "content": "PREV_SESSION_FINAL_REPLY_MARKER_881: all health checks green and rollout complete. " + ("extra detail " * 60)}},
    ]
    tf.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(tf, "claude")
        for i in range(4):
            st.save_note("app", f"Decision note #{i} with detailed architectural guidance for startup budget test", "decision")
        idx = st.index("app", budget_tokens=400)
        assert any(ln.startswith("Last:") and "PREV_SESSION_FINAL_REPLY_MARKER_881" in ln for ln in idx.splitlines())
        assert not any(ln.startswith("Last:") and "Old assistant reply" in ln for ln in idx.splitlines())
        tools_tok = math.ceil(len(json.dumps(TOOL_SPECS, ensure_ascii=False)) / 4)
        idx_tok = math.ceil(len(idx) / 4)
        assert idx_tok + tools_tok <= 450, f"startup exceeded 450 tokens: {idx_tok} + {tools_tok}"
    finally:
        st.close()


def test_r8b_a_dedup_indexed_hash_column_and_migration_backfill(tmp_path):
    import sqlite3
    from mesa_lore.store import MIGRATIONS, SCHEMA_VERSION
    db_path = tmp_path / "v10.db"
    con = sqlite3.connect(db_path)
    with con:
        for ver, step in MIGRATIONS[:10]:
            if isinstance(step, str):
                for s in step.split(";"):
                    if s.strip():
                        con.execute(s)
            else:
                step(con)
            con.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-11-20T00:00:00Z')", (ver,))
        long_txt = " ".join(f"token_migration_backfill_{k}" for k in range(35))
        con.execute(
            "INSERT INTO entries (id, project, text, source_type, session_id, ts, state, dedupe_key, agent) "
            "VALUES ('c_pre1', 'app', ?, 'assistant', 's0', '2026-11-20T09:00:00Z', 'active', 's0:1:0', 'claude')",
            (long_txt,),
        )
        con.execute("INSERT INTO entries_fts (id, text_norm) VALUES ('c_pre1', ?)", (long_txt.lower(),))
    con.close()

    st = Store(db_path)
    try:
        assert st._current_schema_version() == SCHEMA_VERSION >= 11
        cols = {r[1] for r in st.con.execute("PRAGMA table_info(entries)").fetchall()}
        assert "text_hash" in cols
        idxs = {r[1] for r in st.con.execute("PRAGMA index_list(entries)").fetchall()}
        assert "idx_entries_text_hash" in idxs
        h_val = st.con.execute("SELECT text_hash FROM entries WHERE id = 'c_pre1'").fetchone()[0]
        assert isinstance(h_val, bytes) and len(h_val) == 12
        # Confirm query plan uses idx_entries_text_hash (no full scan of scope texts)
        plan = " ".join(str(tuple(x)) for x in st.con.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM entries WHERE project IN ('app') AND text_hash = ? LIMIT 1", (h_val,)
        ).fetchall())
        assert "idx_entries_text_hash" in plan
        # Ingesting a duplicate of the pre-v11 row is skipped via the backfilled text_hash
        tf = tmp_path / "dup.jsonl"
        tf.write_text(json.dumps({
            "type": "assistant", "sessionId": "s1", "cwd": "/repo/app", "timestamp": "2026-11-20T10:00:00Z",
            "message": {"role": "assistant", "content": long_txt.upper()},
        }) + "\n", encoding="utf-8")
        assert st.ingest_file(tf, "claude") == 0
    finally:
        st.close()


def test_r8b_c_skip_non_conflicting_tools_keep_ls_cat_bash_and_subagents(tmp_path):
    tf = tmp_path / "skip_tools.jsonl"
    lines = [
        {"type": "assistant", "sessionId": "s_skip", "cwd": "/repo/app", "timestamp": "2026-11-20T10:00:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {"todos": ["SKIP_TODOWRITE_MARKER"]}},
             {"type": "tool_use", "id": "t2", "name": "Skill", "input": {"skill": "SKIP_SKILL_MARKER"}},
             {"type": "tool_use", "id": "t3", "name": "AskUserQuestion", "input": {"question": "SKIP_ASK_MARKER"}},
             {"type": "tool_use", "id": "t4", "name": "ListMcpResourcesTool", "input": {"server": "SKIP_MCP_RES_MARKER"}},
             {"type": "tool_use", "id": "t5", "name": "SlashCommand", "input": {"command": "SKIP_SLASH_MARKER"}},
             {"type": "tool_use", "id": "t6", "name": "Bash", "input": {"command": "ls -la"}},
             {"type": "tool_use", "id": "t7", "name": "Bash", "input": {"command": "cat config.json"}},
         ]}},
        {"type": "user", "sessionId": "s_skip", "cwd": "/repo/app", "timestamp": "2026-11-20T10:01:00Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "SKIP_TODOWRITE_OUT"},
             {"type": "tool_result", "tool_use_id": "t2", "content": "SKIP_SKILL_OUT"},
             {"type": "tool_result", "tool_use_id": "t3", "content": "SKIP_ASK_OUT"},
             {"type": "tool_result", "tool_use_id": "t4", "content": "SKIP_MCP_RES_OUT"},
             {"type": "tool_result", "tool_use_id": "t5", "content": "SKIP_SLASH_OUT"},
             {"type": "tool_result", "tool_use_id": "t6", "content": "KEEP_LS_OUTPUT_MARKER"},
             {"type": "tool_result", "tool_use_id": "t7", "content": "KEEP_CAT_OUTPUT_MARKER"},
         ]}},
    ]
    tf.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        assert st.ingest_file(tf, "claude") == 2
        texts = "\n".join(r["text"] for r in st.con.execute("SELECT text FROM entries WHERE project = 'app'").fetchall())
        for skip_m in ("SKIP_TODOWRITE", "SKIP_SKILL", "SKIP_ASK", "SKIP_MCP_RES", "SKIP_SLASH"):
            assert skip_m not in texts
        assert "KEEP_LS_OUTPUT_MARKER" in texts
        assert "KEEP_CAT_OUTPUT_MARKER" in texts
    finally:
        st.close()


def test_r8b_d_tool_output_first_400_last_400_cut_marker_and_write_edit_capped_2000(tmp_path):
    tf = tmp_path / "tool_body.jsonl"
    out_head = "HEAD_MARKER_001_" + ("a" * 384)
    out_mid = "MID_DROPPED_OUTPUT_999_" + ("m" * 500)
    out_tail = ("z" * 384) + "_TAIL_MARKER_002"
    raw_out = out_head + out_mid + out_tail
    assert len(out_head) == 400 and len(out_tail) == 400

    write_content = "WRITE_START_MARKER_" + ("w" * 2400) + "_WRITE_END_DROPPED"
    lines = [
        {"type": "assistant", "sessionId": "s_tb", "cwd": "/repo/app", "timestamp": "2026-11-20T10:00:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t_bash", "name": "Bash", "input": {"command": "pytest -v"}},
             {"type": "tool_use", "id": "t_write", "name": "Write", "input": {"file_path": "a.py", "content": write_content}},
         ]}},
        {"type": "user", "sessionId": "s_tb", "cwd": "/repo/app", "timestamp": "2026-11-20T10:01:00Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t_bash", "content": raw_out},
             {"type": "tool_result", "tool_use_id": "t_write", "content": "ok"},
         ]}},
    ]
    tf.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    st = Store(tmp_path / "lore.db")
    try:
        st.ingest_file(tf, "claude")
        rows = [r["text"] for r in st.con.execute("SELECT text FROM entries WHERE project = 'app' ORDER BY rowid").fetchall()]
        joined = "\n".join(rows)
        assert "HEAD_MARKER_001_" in joined
        assert "_TAIL_MARKER_002" in joined
        assert "MID_DROPPED_OUTPUT_999_" not in joined
        assert f"[... {len(out_mid)} chars cut ...]" in joined
        assert "WRITE_START_MARKER_" in joined
        write_pieces = [r for r in rows if "w" * 50 in r]
        assert sum(map(len, write_pieces)) <= 2005 + len("Write \nok")  # 2000 kept + "\n...\n"
    finally:
        st.close()


def test_r8b_d_marker_never_grows_text_and_secret_is_masked_before_cut():
    for n in (0, 1, 800, 801, 820, 5000):
        assert len(trim_tool_output("x" * n, True)) <= n
    for n in (2000, 2001, 2005, 2006, 100_000):
        assert len(trim_tool_output("y" * n)) <= min(n, 2005)
    key = "sk-ant-" + "k" * 900  # crosses the 400-char cut: no half secret may survive
    assert "kkkk" not in trim_tool_output("a" * 395 + key + "b" * 900, True)
