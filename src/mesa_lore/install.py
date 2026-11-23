import json, os, re, shlex, shutil, subprocess, sys
from pathlib import Path
from mesa_lore.readers import parse_transcript
from mesa_lore.store import Store, get_db_path, scan_antigravity

EVENTS = ("SessionStart", "Stop", "SessionEnd")
CODEX_TIMEOUTS = {"SessionStart": 30, "Stop": 5, "SessionEnd": 3}


def _cmd_parts() -> tuple[str, list[str], str]:
    found = shutil.which("mesa-lore") or str(Path(sys.executable).with_name("mesa-lore"))
    cmd, args = (found, []) if Path(found).exists() else (sys.executable, ["-m", "mesa_lore.cli"])
    quoted = subprocess.list2cmdline([cmd]) if sys.platform == "win32" else shlex.quote(cmd)
    return cmd, [*args, "serve"], " ".join([quoted, *args])


def _targets(home: Path) -> dict[str, Path]:
    config = {"darwin": home / "Library/Application Support", "win32": Path(os.environ.get("APPDATA") or home / "AppData/Roaming")}.get(sys.platform, home / ".config")
    desk = config / "Claude/claude_desktop_config.json"
    return {"claude_mcp": home / ".claude.json", "claude_hooks": home / ".claude/settings.json",
            "codex_mcp": home / ".codex/config.toml", "codex_hooks": home / ".codex/hooks.json",
            "ag_mcp": home / ".gemini/antigravity/mcp_config.json", "desktop_mcp": desk}


TOML_BLOCK_RE = re.compile(r"(?ms)^\[mcp_servers\.mesa-lore\]\s*\n.*?(?=^\[|\Z)")

_ours = lambda g: " hook " in (text := json.dumps(g)) and ("mesa-lore" in text or "mesa_lore.cli" in text)  # noqa: E731 - a hook group install added


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _backup_once(path: Path, backup_dir: Path, manifest: dict):
    if (key := str(path)) not in manifest:
        backup = f"{len(manifest)}_{path.name}" if path.exists() else None
        if backup:
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_dir / backup)
        manifest[key] = {"existed": backup is not None, "file": backup}


def run_install(st: Store, home: Path) -> str:
    backup_dir = home / ".config/mesa-lore/backup"
    manifest_path, t = backup_dir / "manifest.json", _targets(home)
    manifest = _read_json(manifest_path)
    for p in t.values():
        _backup_once(p, backup_dir, manifest)
    _write_json(manifest_path, manifest)
    cmd, args, hook_cmd = _cmd_parts()
    for k in ("claude_mcp", "ag_mcp", "desktop_mcp"):
        d = _read_json(t[k])
        d.setdefault("mcpServers", {})["mesa-lore"] = {"command": cmd, "args": args}
        _write_json(t[k], d)

    for k, agent, timeouts in (("claude_hooks", "claude", None), ("codex_hooks", "codex", CODEX_TIMEOUTS)):
        d = _read_json(t[k])
        hooks = d.setdefault("hooks", {})
        for ev in EVENTS:
            groups = hooks.setdefault(ev, [])
            command = f"{hook_cmd} hook {ev} --agent {agent}"
            existing = next((h for g in groups for h in g.get("hooks", []) if _ours(h)), None)
            if existing:
                existing["command"] = command
            else:
                timeout = {"timeout": timeouts[ev]} if timeouts else {}
                groups.append({"hooks": [{"type": "command", "command": command, **timeout}]})
        _write_json(t[k], d)

    toml_p = t["codex_mcp"]
    toml_p.parent.mkdir(parents=True, exist_ok=True)
    cleaned = TOML_BLOCK_RE.sub("", toml_p.read_text(encoding="utf-8") if toml_p.exists() else "").rstrip()
    block = f"[mcp_servers.mesa-lore]\ncommand = {json.dumps(cmd)}\nargs = {json.dumps(args)}\n"
    toml_p.write_text((cleaned + "\n\n" + block) if cleaned else block, encoding="utf-8")

    ingested = scan_antigravity(st)
    for root, ag in ((home / ".claude/projects", "claude"), (home / ".codex/sessions", "codex")):
        for fp in (sorted(root.rglob("*.jsonl")) if root.exists() else ()):
            try:
                ingested += st.ingest_file(fp, agent=ag)
            except Exception:
                pass
    return f"Installed mesa-lore configs (backup: {backup_dir}) and ingested {ingested} chunks."


def run_uninstall(home: Path) -> str:
    """Remove only what install added, from the current files. Backups stay for manual restore."""
    manifest, done = _read_json(home / ".config/mesa-lore/backup/manifest.json"), 0
    for k, p in _targets(home).items():
        if not p.exists():
            continue
        if k == "codex_mcp":
            new = TOML_BLOCK_RE.sub("", p.read_text(encoding="utf-8")).rstrip() + "\n"
        elif not (d := _read_json(p)):
            continue  # unreadable or empty JSON is left alone
        else:
            if isinstance(srv := d.get("mcpServers"), dict) and srv.pop("mesa-lore", None) is not None and not srv:
                del d["mcpServers"]
            if k.endswith("_hooks") and isinstance(d.get("hooks"), dict):
                if not (hooks := {ev: kept for ev, gs in d["hooks"].items() if (kept := [g for g in gs if not _ours(g)])}):
                    del d["hooks"]
                else:
                    d["hooks"] = hooks
            new = json.dumps(d, ensure_ascii=False, indent=2) + "\n"
        if new.strip() in ("", "{}") and not manifest.get(str(p), {}).get("existed", True):
            p.unlink()  # only install created this file
        else:
            p.write_text(new, encoding="utf-8")
        done += 1
    return f"Uninstalled: removed mesa-lore entries from {done} files. Backups kept in {home / '.config/mesa-lore/backup'}."


def run_doctor(st: Store, home: Path) -> str:
    t = _targets(home)
    has_mcp = lambda p: "mesa-lore" in _read_json(p).get("mcpServers", {})
    txt = lambda p: p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    has_hooks = lambda p: _ours(_read_json(p))
    db_p = get_db_path()
    db_sz = f"{db_p.stat().st_size} B" if db_p.exists() else "missing"
    last_ts = (st.con.execute("SELECT MAX(ts) FROM entries").fetchone() or [None])[0] or "never"
    lines = [
        f"Schema version: v{st._current_schema_version()}",
        f"DB: {db_p} ({db_sz}), Last ingest: {last_ts}",
        f"Claude Code MCP: {'OK' if has_mcp(t['claude_mcp']) else 'missing'} | hooks: {'OK' if has_hooks(t['claude_hooks']) else 'missing'}",
        f"Codex MCP: {'OK' if '[mcp_servers.mesa-lore]' in txt(t['codex_mcp']) else 'missing'} | hooks: {'OK (approve with /hooks in Codex)' if has_hooks(t['codex_hooks']) else 'missing'}",
        f"Antigravity MCP: {'OK' if has_mcp(t['ag_mcp']) else 'missing'} | Claude Desktop MCP: {'OK' if has_mcp(t['desktop_mcp']) else 'missing'}",
    ]
    warns = []
    for w in st.warnings():  # a row from an older reader may no longer apply: re-check the file
        found = []
        if not Path(w["path"]).exists():
            with st.con:
                st.con.execute("DELETE FROM ingest_warnings WHERE path = ?", (w["path"],))
            continue
        try:
            parse_transcript(w["path"], w["agent"], warnings=found)
        except OSError:  # unreadable: keep the warning
            found = [w]
        if found:
            warns.append(w)
    lines.append(f"Warnings ({len(warns)}):" if warns else "Warnings: none (0)")
    lines.extend(f"  [WARN] {w['path']} ({w['agent']}): {w['reason']}" for w in warns)
    return "\n".join(lines)
