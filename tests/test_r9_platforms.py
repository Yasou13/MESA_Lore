import json
import shlex
import sqlite3
import subprocess

from mesa_lore import install
from mesa_lore.readers.antigravity import parse_antigravity


def test_windows_uses_roaming_appdata(tmp_path, monkeypatch):
    roaming = tmp_path / "roaming"
    monkeypatch.setattr(install.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(roaming))
    assert install._targets(tmp_path)["desktop_mcp"] == roaming / "Claude/claude_desktop_config.json"


def test_hook_paths_with_spaces_survive_posix_and_windows_shell_quoting(tmp_path, monkeypatch):
    executable = tmp_path / "Program Files" / "mesa-lore"
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setattr(install.shutil, "which", lambda name: str(executable))

    monkeypatch.setattr(install.sys, "platform", "darwin")
    command, args, hook = install._cmd_parts()
    assert args == ["serve"]
    assert shlex.split(f"{hook} hook Stop --agent claude") == [command, "hook", "Stop", "--agent", "claude"]

    monkeypatch.setattr(install.sys, "platform", "win32")
    command, args, hook = install._cmd_parts()
    assert hook == subprocess.list2cmdline([command])


def test_antigravity_decodes_windows_drive_file_uri(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    folder = brain / "conv" / ".system_generated" / "logs"
    folder.mkdir(parents=True)
    transcript = folder / "transcript.jsonl"
    transcript.write_text(json.dumps({"type": "USER_INPUT", "content": "Check workspace."}), encoding="utf-8")
    with sqlite3.connect(tmp_path / "conversation_summaries.db") as db:
        db.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, workspace_uris TEXT, project_id TEXT)")
        db.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)",
                   ("conv", '["file:///C:/My%20Work/app"]', ""))
    monkeypatch.setenv("ANTIGRAVITY_BRAIN_DIR", str(brain))
    chunks, _ = parse_antigravity(transcript)
    assert chunks[0]["cwd"] == "C:/My Work/app"


def test_ci_includes_all_three_operating_systems():
    from pathlib import Path
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    text = workflow.read_text(encoding="utf-8")
    assert all(label in text for label in ("ubuntu-latest", "macos-latest", "windows-latest"))
