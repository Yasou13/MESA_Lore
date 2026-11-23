import json
import os
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mesa_lore.cli import main
from mesa_lore.store import Store


def _desktop_cfg(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def test_install_doctor_uninstall_end_to_end(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "lore.db"

    # Pre-existing configs for some apps (none for Codex hooks.json or Claude Desktop)
    claude_mcp = home / ".claude.json"
    orig_claude_mcp = json.dumps({"theme": "dark", "mcpServers": {"other-mcp": {"command": "other", "args": []}}}, indent=2)
    claude_mcp.write_text(orig_claude_mcp, encoding="utf-8")

    claude_settings = home / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True)
    orig_claude_settings = json.dumps({
        "model": "sonnet",
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo existing"}]}]},
    }, indent=2)
    claude_settings.write_text(orig_claude_settings, encoding="utf-8")

    codex_toml = home / ".codex" / "config.toml"
    codex_toml.parent.mkdir(parents=True)
    orig_codex_toml = 'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "other"\n'
    codex_toml.write_text(orig_codex_toml, encoding="utf-8")

    ag_mcp = home / ".gemini" / "antigravity" / "mcp_config.json"
    ag_mcp.parent.mkdir(parents=True)
    orig_ag_mcp = json.dumps({"mcpServers": {"ctx7": {"command": "npx"}}}, indent=2)
    ag_mcp.write_text(orig_ag_mcp, encoding="utf-8")

    codex_hooks = home / ".codex" / "hooks.json"
    desktop_cfg = _desktop_cfg(home)

    # Pre-existing history transcripts
    cl_hist = home / ".claude" / "projects" / "proj1" / "s1.jsonl"
    cl_hist.parent.mkdir(parents=True)
    cl_hist.write_text(json.dumps({
        "type": "user", "sessionId": "cl-h1", "cwd": "/repo/proj1", "timestamp": "2026-11-10T10:00:00Z",
        "message": {"role": "user", "content": "webhook retry backoff 5s"},
    }) + "\n", encoding="utf-8")

    cx_hist = home / ".codex" / "sessions" / "2026" / "11" / "10" / "rollout-1.jsonl"
    cx_hist.parent.mkdir(parents=True)
    cx_hist.write_text("\n".join([
        json.dumps({"type": "session_meta", "timestamp": "2026-11-10T11:00:00Z", "payload": {"id": "cx-h1", "cwd": "/repo/proj2"}}),
        json.dumps({"type": "response_item", "timestamp": "2026-11-10T11:00:01Z",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fatura queue config"}]}}),
    ]) + "\n", encoding="utf-8")

    ag_hist = home / ".gemini" / "antigravity" / "brain" / "ag-h1" / ".system_generated" / "logs" / "transcript.jsonl"
    ag_hist.parent.mkdir(parents=True)
    ag_hist.write_text(json.dumps({
        "type": "USER_INPUT", "step_index": 0, "created_at": "2026-11-10T12:00:00Z",
        "content": "<user_information>\nActive Workspaces:\n- /repo/proj3\n</user_information>\nantigravity note sync",
    }) + "\n", encoding="utf-8")

    def run_cli(*argv):
        buf = StringIO()
        env = {"MESA_LORE_DB": str(db), "ANTIGRAVITY_BRAIN_DIR": str(home / ".gemini" / "antigravity" / "brain")}
        with patch.dict(os.environ, env):
            with patch.object(sys, "argv", ["mesa-lore", *argv]):
                with patch("sys.stdout", buf):
                    main()
        return buf.getvalue()

    # 1. Install (and verify idempotency on second install)
    inst_out = run_cli("install", "--home", str(home))
    assert "Installed" in inst_out or "configured" in inst_out.lower()
    run_cli("install", "--home", str(home))

    c_mcp = json.loads(claude_mcp.read_text(encoding="utf-8"))
    assert c_mcp["theme"] == "dark"
    assert "other-mcp" in c_mcp["mcpServers"] and "mesa-lore" in c_mcp["mcpServers"]

    c_set = json.loads(claude_settings.read_text(encoding="utf-8"))
    assert c_set["model"] == "sonnet"
    assert c_set["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo existing"
    assert sum("mesa-lore hook SessionStart" in json.dumps(g) for g in c_set["hooks"]["SessionStart"]) == 1
    assert "Stop" in c_set["hooks"] and "SessionEnd" in c_set["hooks"]

    toml_txt = codex_toml.read_text(encoding="utf-8")
    assert 'model = "gpt-5"' in toml_txt and "[mcp_servers.other]" in toml_txt and "[mcp_servers.mesa-lore]" in toml_txt

    cx_h = json.loads(codex_hooks.read_text(encoding="utf-8"))
    assert cx_h["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] == 30
    assert "timeoutSec" not in codex_hooks.read_text(encoding="utf-8")

    assert "ctx7" in json.loads(ag_mcp.read_text(encoding="utf-8"))["mcpServers"]
    assert "mesa-lore" in json.loads(ag_mcp.read_text(encoding="utf-8"))["mcpServers"]
    assert "mesa-lore" in json.loads(desktop_cfg.read_text(encoding="utf-8"))["mcpServers"]

    # History was ingested
    st = Store(db)
    try:
        assert st.search("webhook", "proj1", budget_tokens=300)["status"] == "ok"
        assert st.search("fatura", "proj2", budget_tokens=300)["status"] == "ok"
        assert st.search("sync", "proj3", budget_tokens=300)["status"] == "ok"
    finally:
        st.close()

    # 2. Doctor shows hooks, 4 MCP configs, DB stats, schema version, and warnings
    doc_out = run_cli("doctor", "--home", str(home))
    assert "Schema version: v" in doc_out
    assert "Warnings: none (0)" in doc_out
    for label in ("Claude Code", "Codex", "Antigravity", "Claude Desktop", "Last ingest"):
        assert label in doc_out
    assert "/hooks" in doc_out

    # 3. Uninstall restores originals and removes newly created config files
    uninst_out = run_cli("uninstall", "--home", str(home))
    assert "Restored" in uninst_out or "uninstalled" in uninst_out.lower()
    # C4-6: uninstall edits the current files (JSON is re-serialized), so compare content, not bytes.
    assert json.loads(claude_mcp.read_text(encoding="utf-8")) == json.loads(orig_claude_mcp)
    assert json.loads(claude_settings.read_text(encoding="utf-8")) == json.loads(orig_claude_settings)
    assert codex_toml.read_text(encoding="utf-8") == orig_codex_toml
    assert json.loads(ag_mcp.read_text(encoding="utf-8")) == json.loads(orig_ag_mcp)
    assert not codex_hooks.exists()
    assert not desktop_cfg.exists()
    assert db.exists()
