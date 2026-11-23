import argparse, hashlib, json, os, re, shutil, sqlite3, sys, time, unicodedata
from contextlib import closing
from pathlib import Path
from mesa_lore.store import Store, _fields, _parse_ts, _project_scope, get_db_path, get_project, scan_antigravity
from mesa_lore.text import mask_secrets, wrap_untrusted

def get_queue_path() -> Path:
    return Path(q) if (q := os.environ.get("MESA_LORE_QUEUE")) else get_db_path().parent / "queue.txt"

def _resolve(item: str):
    """Queue item -> (path, agent). Items are transcript paths or bare session ids."""
    p = Path(item)
    if p.is_file():
        return p, "codex" if ".codex" in str(p) or p.name.startswith("rollout-") else "claude"
    if not p.exists() and re.fullmatch(r"[\w-]+", item):
        for agent, pattern in (("codex", f".codex/sessions/**/*{item}*.jsonl"),
                               ("claude", f".claude/projects/**/*{item}*.jsonl")):
            match = next(Path.home().glob(pattern), None)
            if match:
                return match, agent
    return None, None

def ingest_pending_files(st: Store) -> int:
    q_path = get_queue_path()
    work = q_path.with_name(f"{q_path.name}.{os.getpid()}")
    try:
        os.replace(q_path, work)  # hooks that run meanwhile start a fresh queue file
    except FileNotFoundError:
        return 0
    items = dict.fromkeys(x.strip() for x in work.read_text(encoding="utf-8").splitlines() if x.strip())
    total = 0
    for item in items:  # Stop fires every turn, so the same transcript is queued many times
        path, agent = _resolve(item)
        if path:
            try:
                total += st.ingest_file(path, agent=agent)
            except (OSError, ValueError):
                pass
    work.unlink()
    q_path.touch()
    return total

def _read_hook_json() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}

def _handle_end_hook():
    data = _read_hook_json()
    transcript = next((data[k] for k in ("transcript_path", "transcriptPath", "rollout_path", "session_path",
                                         "sessionPath", "session_id", "sessionId", "thread_id", "id", "path")
                       if isinstance(data.get(k), str) and data[k].strip()), None)
    if transcript and "\n" not in transcript:
        q_path = get_queue_path()
        q_path.parent.mkdir(parents=True, exist_ok=True)
        with open(q_path, "a", encoding="utf-8") as f:
            f.write(f"{transcript.strip()}\n")
    return data

def _handoff_reason(data: dict) -> str | None:
    """Stop-hook block reason, or None. Any doubt (no flag, odd id, no or busy DB) lets the agent stop."""
    sid = data.get("session_id")
    if data.get("stop_hook_active") is not False or not isinstance(sid, str) or not re.fullmatch(r"[\w-]+", sid) or not get_db_path().exists():
        return None
    try:
        with closing(Store(get_db_path())) as st:
            st.con.execute("PRAGMA busy_timeout = 1000")
            due = st.handoff_due(sid)
    except sqlite3.Error:
        return None
    return (f"Before stopping, call memory_save with kind=handoff, session_id={sid}. Text: max 4 lines, "
            "Done / Decisions / Open / Next, no file dumps, under 120 tokens. Then stop.") if due else None

def _handle_start_hook(st: Store):
    data = _read_hook_json()
    ingest_pending_files(st)
    idx = st.index(get_project(data.get("cwd") or os.getcwd()))
    if idx:  # JSON form is understood by both Claude Code and Codex SessionStart hooks
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": wrap_untrusted(idx)}}, ensure_ascii=False))

def _export(args, parser):
    if args.all and (not args.out or args.project):
        parser.error("--all needs -o FOLDER and no --project")
    try:
        with closing(Store(get_db_path(), read_only=True)) as st:
            if not args.all:
                md = st.export(args.project or get_project(os.getcwd()))
                Path(args.out).write_text(md, encoding="utf-8") if args.out else sys.stdout.write(md)
                return
            Path(args.out).mkdir(parents=True, exist_ok=True)
            for p in sorted({_project_scope(r[0])[0] for r in st.con.execute("SELECT project FROM entries UNION SELECT project FROM handoffs").fetchall()}):
                safe = re.sub(r"[^\w.-]", "_", p)[:100]  # project names come from notes: never a path
                name = safe if safe == p else f"{safe}-{hashlib.sha256(p.encode()).hexdigest()[:6]}"
                (Path(args.out) / f"{name}.md").write_text(st.export(p), encoding="utf-8")
    except sqlite3.OperationalError as e:
        sys.exit(f"export: cannot read {get_db_path()} ({e}). Run `mesa-lore doctor` to create or migrate it.")

_EMPTY = "No memory yet. Run `mesa-lore install`."
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")  # stored text must not be able to drive the terminal
_section = lambda title, rows: ["", title, *(rows or ["  (none)"])]
_cols = lambda c: 0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
_width = lambda s: sum(map(_cols, s))

def _fit(text, width):
    """One clean line of at most `width` columns. Masks first: a cut secret would no longer match."""
    text = " ".join(_CONTROL.sub(" ", mask_secrets(text or "")).replace("**", "").split())
    text = text[:-3].rstrip() + "…" if text.endswith("...") else text
    if _width(text) <= width:
        return text
    end = used = 0
    while used + _cols(text[end]) < width:
        used, end = used + _cols(text[end]), end + 1
    return text[:end].rstrip() + "…"

def _ago(ts):
    age = time.time() - t if (t := ts if isinstance(ts, float) else _parse_ts(ts)) else None  # scopes() gives epoch seconds
    for size, unit in ((31536000, "year"), (2592000, "month"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if age is not None and age >= size:
            return f"{int(age // size)} {unit}{'s' if age >= 2 * size else ''} ago"
    return "just now" if age is not None else "unknown"

def _overview(st, scopes, width):
    you = [n for title, items in st.sections("global", 0).items() if title not in ("Sessions", "History") for n in items if n["kind"] != "handoff"]
    out = _section("YOU", [f"  {_fit(n['text'], width - 2)}" for n in you])[1:]
    for title, group in (("PROJECTS", [s for s in scopes if s[0] != "global" and not s[0].startswith("research:")]),
                         ("RESEARCH", [s for s in scopes if s[0].startswith("research:")])):
        names, rows = [_fit(name, width // 4) for name, _ in group], []
        name_w = max(map(_width, names), default=0)
        for shown, (name, ts) in zip(names, group):
            h = st.latest_handoff(name)
            info = _fields(h["text"]) if h else {}
            key = next((k for k in ("Next", "Open", "Done") if info.get(k)), "")  # auto handoffs have no Next line
            meta = _fit(f"{_fit(h['agent'] or 'unknown', 12)} · {_ago(ts)}" if h else _ago(ts), width // 3)
            body = _fit(info.get(key), width - name_w - _width(meta) - 12) if key else ""
            left = f"  {shown}{' ' * (name_w - _width(shown))}  " + (f"{key}: {body}" if key else "")
            rows.append(f"{left}{' ' * (width - _width(left) - _width(meta))}{meta}")
        out += _section(title, rows)
    return "\n".join(out)

def _project_view(st, name, sessions, width):
    data, h = st.sections(name, max(sessions, 0)), st.latest_handoff(name)
    todos, pickup = data["Open items"], []
    if h:
        info = _fields(h["text"])
        if "id" not in h:  # auto text holds a copy of the todo from ingest time: show the live one
            info["Open"] = todos[-1]["text"] if todos else ""
        pickup = [f"  {_fit(' · '.join((h['agent'] or 'unknown', 'written by agent' if 'id' in h else 'auto', _ago(h['ts']))), width - 2)}"]
        pickup += [f"  {k:<11}{_fit(info[k], width - 13)}" for k in ("Done", "Decisions", "Open", "Next") if info.get(k)] or [f"  {_fit(h['text'], width - 2)}"]
    out = [name, *_section("PICK UP WHERE YOU LEFT OFF", pickup)]
    out += _section("OPEN ITEMS", [f"  {i}. {_fit(n['text'], width - 4 - len(str(i)))}" for i, n in enumerate(todos, 1)])
    out += _section("DECISIONS", [f"  {n['id'][:8]}  {_fit(n['text'], width - 12)}" for n in data["Decisions"]])
    rows = []
    for s in data["Sessions"]:
        rows.append(f"  {(s['ts'] or 'unknown')[:10]}  {_fit(s['agent'] or 'unknown', width - 14)}")
        rows += [f"    {k:<7}{_fit(v, width - 11)}" for k, v in (("Asked", s["asked"]), ("Done", _fields(s["text"]).get("Done"))) if v]
    return "\n".join(out + _section("SESSIONS", rows))

def _view(args):
    if not get_db_path().exists():
        return print(_EMPTY)
    try:
        with closing(Store(get_db_path(), read_only=True)) as st:
            scopes, width = st.scopes(), max(shutil.get_terminal_size().columns, 40)
            name = args.project and (get_project(os.getcwd()) if args.project == "." else _project_scope(args.project)[0])
            if not scopes or (name and name not in dict(scopes)):
                return print(f"No memory for {name}." if scopes else _EMPTY)
            print(_project_view(st, name, args.sessions, width) if name else _overview(st, scopes, width))
    except sqlite3.OperationalError as e:
        sys.exit(f"view: cannot read {get_db_path()} ({e}). Run `mesa-lore doctor` to create or migrate it.")

def main():
    parser = argparse.ArgumentParser(prog="mesa-lore", description="Local memory layer for coding agents")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Ingest a transcript file")
    p_ingest.add_argument("file", help="Path to transcript file")
    p_ingest.add_argument("--agent", choices=["claude", "codex", "antigravity"], required=True, help="Agent type")
    p_search = sub.add_parser("search", help="Search memory")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--budget", type=int, default=800, help="Token budget")
    p_search.add_argument("--kind", choices=["decision", "fact", "todo", "failed_attempt", "handoff"], default=None, help="Filter by note kind")
    p_index = sub.add_parser("index", help="Show session-start memory index")
    p_index.add_argument("--budget", type=int, default=400, help="Token budget")
    p_view = sub.add_parser("view", help="Readable overview, or one project (. = current folder)")
    p_view.add_argument("project", nargs="?", help="Project name")
    p_view.add_argument("--sessions", type=int, default=5, help="Sessions to show")
    p_show = sub.add_parser("show", help="Show entry details")
    p_show.add_argument("id", help="Entry ID")
    p_show.add_argument("--before", type=int, default=0, help="Neighbor chunks before")
    p_show.add_argument("--after", type=int, default=0, help="Neighbor chunks after")
    sub.add_parser("delete", help="Delete entry by ID").add_argument("id", help="Entry ID")
    p_purge = sub.add_parser("purge", help="Purge entries for a project or older than age")
    p_purge.add_argument("--older-than", default=None, help="Age cutoff e.g. 90d")
    p_export = sub.add_parser("export", help="Export markdown view of project notes")
    p_export.add_argument("--project", "-p", help="Project name")
    p_export.add_argument("--out", "-o", help="Output file (with --all: output folder)")
    p_export.add_argument("--all", action="store_true", help="Write one markdown file per project into the -o folder")
    p_stats = sub.add_parser("stats", help="Show token usage statistics")
    p_stats.add_argument("--project", default=None, help="Filter by project")
    p_stats.add_argument("--days", type=int, default=None, help="Filter to last N days")
    p_stats.add_argument("--agent", default=None, help="Filter by agent")
    p_stats.add_argument("--json", action="store_true", help="Output as JSON")
    for p_cmd in (p_search, p_index, p_show, p_purge):
        p_cmd.add_argument("--project", default=None, help="Project name")
    sub.add_parser("serve", help="Run MCP stdio server")
    for name, hlp in (("ingest-pending", "Ingest all queued transcripts"), ("install", "Configure MCP/hooks and ingest history"),
                      ("doctor", "Check installation, schema and ingest warnings"), ("uninstall", "Restore backed-up configs")):
        sub.add_parser(name, help=hlp).add_argument("--home", default=None, help="Override HOME directory")
    p_hook = sub.add_parser("hook", help="Handle agent hook events")
    p_hook.add_argument("event", help="Hook event name")
    p_hook.add_argument("--agent", choices=["claude", "codex"], required=True, help="Agent type")

    args = parser.parse_args()
    home = Path(args.home).resolve() if getattr(args, "home", None) else Path.home()
    if getattr(args, "home", None):
        os.environ.update(HOME=str(home), USERPROFILE=str(home))
        if sys.platform == "win32":
            os.environ["APPDATA"] = str(home / "AppData/Roaming")
    if args.command == "serve":
        from mesa_lore.mcp_server import run_server
        run_server()
        return
    if args.command == "uninstall":
        from mesa_lore.install import run_uninstall
        print(run_uninstall(home))
        return
    if args.command == "hook" and args.event in ("SessionEnd", "Stop"):
        data = _handle_end_hook()
        if args.event == "Stop" and (reason := _handoff_reason(data)):
            print(json.dumps({"decision": "block", "reason": reason}))
        return
    if args.command == "export":
        return _export(args, parser)
    if args.command == "view":
        return _view(args)

    st = Store(get_db_path())
    try:
        if args.command == "ingest":
            print(f"Ingested {st.ingest_file(args.file, agent=args.agent)} new chunks.")
        elif args.command == "ingest-pending":
            print(f"Ingested {ingest_pending_files(st) + scan_antigravity(st)} pending chunks.")
        elif args.command == "search":
            p = args.project or get_project(os.getcwd())
            res = st.search(args.query, p, budget_tokens=args.budget, kind=args.kind)
            if res["status"] == "no_match" or not res["results"]:
                print("No match found.")
            else:
                for r in res["results"]:
                    stale_flag = " [STALE]" if r.get("stale_hint") else ""
                    print(f"[{r['id']}] ({r['source_type']}{stale_flag}) {r['text']}")
        elif args.command == "index":
            print(st.index(args.project or get_project(os.getcwd()), budget_tokens=args.budget) or "No memory index.")
        elif args.command == "show":
            e = st.get(args.id, project=args.project or get_project(os.getcwd()), before=args.before, after=args.after)
            print("\n".join(f"{k}: {v}" for k, v in e.items()) if e else f"Not found: {args.id}")
        elif args.command == "delete":
            print(f"Deleted {args.id}: {st.delete(args.id)}")
        elif args.command == "purge":
            if args.older_than:
                m = re.fullmatch(r"(\d+)d?", args.older_than.strip())
                if not m:
                    parser.error(f"Invalid --older-than value: {args.older_than}")
                print(f"Purged {st.purge_older_than(int(m.group(1)), project=args.project)} entries older than {args.older_than}.")
            elif args.project:
                print(f"Purged {st.purge(args.project)} entries for project {args.project}.")
            else:
                parser.error("purge requires --project or --older-than")
        elif args.command == "stats":
            data = st.stats(project=args.project, days=args.days, agent=args.agent)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            elif not data["agents"]:
                print("No usage data found.")
            else:
                print(f"{'Agent':<12} {'Sess':>5} {'Turns':>6} {'Uncached':>10} {'CacheRead':>11} {'CacheWrite':>10} {'Output':>8} {'Avg/Turn':>9} {'Resumes':>7} {'AvgResume':>10}")
                for a in data["agents"]:
                    print(f"{a['agent']:<12} {a['sessions']:>5} {a['turns']:>6} {a['uncached_input']:>10} {a['cache_read']:>11} {a['cache_write']:>10} {a['output']:>8} {a['avg_tokens_per_turn']:>9} {a['resume_turns']:>7} {a['avg_resume_cost']:>10}")
                print(f"\n{'Memory (uncached+write)':<24} {'Sess':>5} {'Turns':>6} {'Avg/Turn':>10} {'Avg/Start':>10} {'First10':>10} {'Avg/Resume':>10}")
                for label, k in (("With memory", "with_memory"), ("Without memory", "without_memory"), ("With handoff", "with_handoff")):
                    c = data["memory_comparison"][k]
                    print(f"{label:<24} {c['sessions']:>5} {c['turns']:>6} {c['avg_turn_cost']:>10} {c['avg_session_start_cost']:>10} {c['avg_first_10_cost']:>10} {c['avg_resume_cost']:>10}")
                print(f"\nNotes: {data['notes_count']} ({data['notes_per_session']}/session)")
        elif args.command in ("install", "doctor"):
            from mesa_lore.install import run_doctor, run_install
            print(run_install(st, home) if args.command == "install" else run_doctor(st, home))
        elif args.command == "hook":
            if args.event == "SessionStart":
                _handle_start_hook(st)
    finally:
        st.close()

if __name__ == "__main__":
    main()
