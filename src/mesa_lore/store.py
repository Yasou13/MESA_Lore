import ast, hashlib, json, math, os, re, sqlite3, subprocess, time, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from mesa_lore.readers import has_session_start_ctx, parse_transcript
from mesa_lore.readers.antigravity import discover_transcripts
from mesa_lore.readers.codex import clean_codex_request
from mesa_lore.text import FOLD, normalize, fold_tr, tr_stem, mask_secrets, STOP_WORDS, TR_STOP_WORDS, split_text, KIND_LABELS

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (id TEXT PRIMARY KEY, project TEXT NOT NULL, text TEXT NOT NULL, source_type TEXT NOT NULL,
    session_id TEXT, ts TEXT, state TEXT NOT NULL DEFAULT 'active', verified INTEGER, stale_hint INTEGER DEFAULT 0,
    kind TEXT, quote TEXT, replaces TEXT, files TEXT, repo TEXT, file_hashes TEXT, dedupe_key TEXT UNIQUE, agent TEXT, model TEXT);
CREATE INDEX IF NOT EXISTS idx_entries_project ON entries(project);
CREATE INDEX IF NOT EXISTS idx_entries_session ON entries(session_id);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(id UNINDEXED, text_norm, tokenize='trigram');
CREATE TABLE IF NOT EXISTS usage (session_id TEXT NOT NULL, agent TEXT NOT NULL, project TEXT NOT NULL, ts TEXT,
    uncached_input INTEGER NOT NULL, cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL,
    output INTEGER NOT NULL, memory_used INTEGER NOT NULL DEFAULT 0, handoff_used INTEGER NOT NULL DEFAULT 0, dedupe_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"""
V2_WARNINGS = "CREATE TABLE IF NOT EXISTS ingest_warnings (path TEXT NOT NULL, agent TEXT NOT NULL, reason TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY (path, agent, reason));"
V3_INGESTED_FILES = "CREATE TABLE IF NOT EXISTS ingested_files (path TEXT NOT NULL, agent TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, PRIMARY KEY (path, agent));"
V4_HANDOFFS = "CREATE TABLE IF NOT EXISTS handoffs (id TEXT PRIMARY KEY, project TEXT NOT NULL, text TEXT NOT NULL, session_id TEXT NOT NULL, ts TEXT, state TEXT NOT NULL DEFAULT 'active', agent TEXT, UNIQUE(project, session_id));"
def _v5_usage_handoff(con):
    if "handoff_used" not in {r[1] for r in con.execute("PRAGMA table_info(usage)").fetchall()}:
        con.execute("ALTER TABLE usage ADD COLUMN handoff_used INTEGER NOT NULL DEFAULT 0")

def _fts_norm(s: str) -> str:
    f = fold_tr(n := normalize(s))
    return f"{n}\n{f}" if f != n else n

def _v6_index_handoffs(con):
    con.execute(V4_HANDOFFS)
    for r in con.execute("SELECT id, text, agent FROM handoffs WHERE id NOT IN (SELECT id FROM entries_fts)").fetchall():
        con.execute("INSERT INTO entries_fts (id, text_norm) VALUES (?, ?)", (r[0], normalize(f"{KIND_LABELS['handoff']} {r[2] or ''} {r[1]}".strip())))  # v7 folds

def _v7_fold_fts(con):
    for rid, txt in con.execute("SELECT rowid, text_norm FROM entries_fts").fetchall():
        if (f := fold_tr(txt)) != txt and f not in txt:
            con.execute("UPDATE entries_fts SET text_norm = ? WHERE rowid = ?", (f"{txt}\n{f}", rid))

V8_SEARCH_INDEXES = ("CREATE INDEX IF NOT EXISTS idx_entries_replaces ON entries(replaces) WHERE replaces IS NOT NULL;"  # search pair lookup
                     "CREATE INDEX IF NOT EXISTS idx_fts_content_id ON entries_fts_content(c0);"  # entry id -> FTS rowid, no text read
                     "CREATE INDEX IF NOT EXISTS idx_entries_scope ON entries(project, id, source_type, kind);"  # scope filter, no text read
                     "INSERT INTO entries_fts(entries_fts) VALUES('optimize');")  # one FTS segment: -25% MATCH time on the real DB
V9_DROP_CANDIDATES = ("DELETE FROM entries_fts WHERE id IN (SELECT id FROM entries WHERE state = 'candidate');"  # feature removed in R7: no recall effect
                      "DELETE FROM entries WHERE state = 'candidate'")
V10_STOP_TURNS = "CREATE TABLE IF NOT EXISTS stop_turns (session_id TEXT PRIMARY KEY, turns INTEGER NOT NULL DEFAULT 0, next_block INTEGER NOT NULL DEFAULT 8);"
_nh = lambda s: hashlib.sha256(" ".join(normalize(s).split()).encode()).digest()[:12]

def _v11_text_hash(con):
    if "text_hash" not in {r[1] for r in con.execute("PRAGMA table_info(entries)").fetchall()}:
        con.execute("ALTER TABLE entries ADD COLUMN text_hash BLOB")
    con.execute("CREATE INDEX IF NOT EXISTS idx_entries_text_hash ON entries(project, text_hash) WHERE text_hash IS NOT NULL")
    rows = con.execute("SELECT id, text FROM entries WHERE source_type NOT IN ('note', 'user') AND LENGTH(text) >= 200 AND text_hash IS NULL").fetchall()
    con.executemany("UPDATE entries SET text_hash = ? WHERE id = ?", [(_nh(r[1]), r[0]) for r in rows])

_SAVED_HANDOFF = re.compile(r"\S*memory_save .*Saved note: (n_\w+) \(kind: handoff", re.S)  # only the memory_save call itself may link a note

def _link_handoffs(con, tool_rows):
    for sid, text in tool_rows:
        if m := _SAVED_HANDOFF.match(text):
            con.execute("UPDATE entries SET session_id = ? WHERE id = ? AND kind = 'handoff' AND session_id IS NULL", (sid, m[1]))

_v12_link_handoffs = lambda con: _link_handoffs(con, con.execute("SELECT session_id, text FROM entries WHERE source_type = 'tool' AND text LIKE '%memory_save %(kind: handoff%'").fetchall())
SCHEMA_VERSION = 12
MIGRATIONS = [(1, SCHEMA), (2, V2_WARNINGS), (3, V3_INGESTED_FILES), (4, V4_HANDOFFS), (5, _v5_usage_handoff), (6, _v6_index_handoffs), (7, _v7_fold_fts),
              (8, V8_SEARCH_INDEXES), (9, V9_DROP_CANDIDATES), (10, V10_STOP_TURNS), (11, _v11_text_hash), (12, _v12_link_handoffs)]
ENTRY_COLS = "id, project, text, source_type, kind, session_id, ts, state, verified, stale_hint, repo, file_hashes, agent, model"
HANDOFF_COLS = "handoffs.id, handoffs.project, handoffs.text, 'note' AS source_type, 'handoff' AS kind, handoffs.session_id, handoffs.ts, handoffs.state, 0 AS verified, 0 AS stale_hint, NULL AS repo, NULL AS file_hashes, handoffs.agent, NULL AS model"

def _load_config(home: Path | None = None) -> tuple[set[str], dict[str, str]]:
    cfg = (home or Path.home()) / ".config" / "mesa-lore" / "config.toml"
    if not cfg.exists():
        return set(), {}
    raw = cfg.read_text(encoding="utf-8", errors="replace")
    try:
        import tomllib
        data = tomllib.loads(raw)
        vals, als = data.get("excluded_projects") or [], data.get("aliases") or {}
    except Exception:
        clean = re.sub(r"(?m)^\s*#.*$", "", raw)
        m = re.search(r"excluded_projects\s*=\s*(\[[^\]]*\])", clean)
        vals = ast.literal_eval(m.group(1)) if m else []
        sec = re.search(r"(?s)\[aliases\]\s*(.*?)(?=\n\s*\[|\Z)", clean)
        als = {k.strip("\"' "): v.strip("\"' ") for k, v in re.findall(r'^([^\s=]+)\s*=\s*["\']([^"\']+)["\']', sec.group(1), re.M)} if sec else {}
    excl = {str(v).strip() for v in vals if isinstance(v, str) and str(v).strip()}
    aliases = {str(k).strip(): str(v).strip() for k, v in als.items() if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()} if isinstance(als, dict) else {}
    return excl, aliases

_load_excluded_projects = lambda home=None: _load_config(home)[0]
_in_sql = lambda col, vals: f"{col} IN ({','.join('?' * len(vals))})"

def _project_scope(project: str) -> tuple[str, ...]:
    aliases = _load_config()[1]
    canon = aliases.get(project, project)
    return (canon, *(k for k, v in aliases.items() if v == canon and k != canon))

def get_db_path() -> Path:
    return Path(p) if (p := os.environ.get("MESA_LORE_DB")) else Path.home() / ".local" / "share" / "mesa-lore" / "lore.db"

def _repo_at(cwd):
    if not cwd:
        return None
    try:
        p = Path(cwd).resolve()
        while not p.is_dir() and p != p.parent:
            p = p.parent
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        res = subprocess.run(["git", "rev-parse", "--show-toplevel", "--git-common-dir"], cwd=p, capture_output=True, text=True, timeout=2.0, env=env)
        if res.returncode == 0:
            root, git_dir = res.stdout.splitlines()
            common = (p / git_dir).resolve()
            return Path(root), common.parent.name if common.name == ".git" else Path(root).name
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

def get_project(cwd: str | None) -> str:
    return _project_scope(repo[1])[0] if (repo := _repo_at(cwd)) else "misc"

def get_git_commit(repo: str, file_path: str) -> str | None:
    try:
        res = subprocess.run(["git", "log", "-1", "--format=%H", "--", file_path], cwd=repo, capture_output=True, text=True, timeout=2.0)
        return res.stdout.strip() if res.returncode == 0 else None
    except Exception:
        return None

def check_stale(repo: str | None, file_hashes_json: str | None, cache: dict | None = None) -> bool:
    """cache: {(repo, file): commit} shared within one call, so N notes on one file run `git log` once."""
    try:
        hashes = json.loads(file_hashes_json) if repo and file_hashes_json else None
        cache = {} if cache is None else cache
        for f, old in (hashes.items() if isinstance(hashes, dict) else ()):
            if (repo, f) not in cache:
                cache[(repo, f)] = get_git_commit(repo, f)
            if cache[(repo, f)] is not None and cache[(repo, f)] != old:
                return True
        return False
    except Exception:
        return False

# same-topic rule for replace hints: a change cue + >= 2 shared 5-char stems covering >= half of the shorter note
SUPERSEDE_CUE_RE = re.compile(r"\b(?:yerine|artik|gecildi|gectik|secildi|degistirildi|iptal|tamamlan\w*|cozuldu|bitti|replaced|switched|changed|migrated|instead|done|fixed|resolved)\b")
SLASH_WRAP_RE = re.compile(r"(?s)<(command-(?:name|message|args)|local-command-(?:stdout|stderr|caveat))>.*?</\1>")
_sig_words = lambda s: {fold_tr(w)[:5] for w in re.findall(r"\w+", normalize(s)) if len(w) >= 3 and w not in STOP_WORDS}
_clip = lambda s, n: s if len(s) <= n else s[:n - 3].rstrip() + "..."

_SLASH_TAG = re.compile(r"</?(?:command-(?:name|message|args)|local-command-(?:stdout|stderr|caveat))\b[^<>]*>")
_MARKUP = re.compile(r"</?[\w-]+(?:\s[^<>]*)?>")
_EVAL_PREFIXES = ("you are the answering model", "you are a blind judge")
_EVAL_RESULT = re.compile(r"\b(?:(?:ans_out|judge_out)_\d+\.json|delta_(?:prim|sec)_out)", re.I)

def _clean_user_prompt(text):
    return _SLASH_TAG.sub(" ", SLASH_WRAP_RE.sub(" ", text)).strip()

def _one_line(text):
    return " ".join(_MARKUP.sub(" ", text).split())

def _handoff_text(asked="", done="", files="", errors="", open_item=""):
    done = " ".join(re.split(r"(?<=[.!?])\s+", mask_secrets(done), maxsplit=2)[:2])
    return "\n".join(f"{label}: {_clip(mask_secrets(value), cap)}" for label, value, cap in (
        ("Asked", asked, 100), ("Done", done, 140), ("Files", files, 115), ("Errors", errors, 55), ("Open", open_item, 33),
    ) if value)

_fields = lambda text: dict(ln.split(": ", 1) for ln in (text or "").splitlines() if ": " in ln)

def _shown_handoff(text, agent):
    legacy = _fields(text)
    if "İstek" not in legacy and "Yapılan" not in legacy:
        return mask_secrets(text)
    asked = legacy.get("İstek", "").split(" | ", 1)[0]
    if agent == "codex":
        asked = clean_codex_request(asked.replace(" ## My request:", "\n## My request:", 1))
    done = _one_line(legacy.get("Yapılan", "").split(" | ")[-1])
    if asked.casefold().startswith(_EVAL_PREFIXES) or (not asked and _EVAL_RESULT.search(done)):
        return ""
    return _handoff_text(_one_line(asked), done, open_item=_one_line(legacy.get("Açık todo", "")))

def _row_to_dict(r, cache: dict | None = None) -> dict:
    stale = check_stale(r["repo"], r["file_hashes"], cache) or bool(r["stale_hint"])
    return {k: (bool(r[k]) if k == "verified" and r[k] is not None else (stale if k == "stale_hint" else r[k]))
            for k in ("id", "project", "text", "source_type", "kind", "session_id", "ts", "state", "verified", "stale_hint", "agent", "model")}

def _parse_ts(ts: str | None) -> float | None:
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00")).timestamp() if ts and isinstance(ts, str) else None
    except ValueError:
        return None

def ingest_all(st, paths, agent) -> int:
    cnt = 0
    for p in paths:
        try:
            cnt += st.ingest_file(p, agent=agent)
        except Exception:
            pass
    return cnt

scan_antigravity = lambda st: ingest_all(st, discover_transcripts(), "antigravity")

class Store:
    def __init__(self, db_path, read_only=False):
        if db_path != ":memory:" and not read_only:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro" if read_only else str(db_path), uri=read_only, timeout=30.0)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA busy_timeout = 30000")
        if read_only:  # no create, no migration, no journal change
            return
        for attempt in range(10):
            try:
                if self.con.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                    self.con.execute("PRAGMA journal_mode = WAL")
                if (ver := self._current_schema_version()) < SCHEMA_VERSION:
                    self._init_schema(ver)
                break
            except sqlite3.OperationalError:
                if attempt == 9:
                    raise
                time.sleep(0.02 * (attempt + 1))

    def _current_schema_version(self) -> int:
        if not self.con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone():
            return 0
        return self.con.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]

    def _init_schema(self, current_ver: int = 0):
        with self.con:
            now = datetime.now(timezone.utc).isoformat()
            for ver, step in MIGRATIONS:
                if ver > current_ver:
                    if isinstance(step, str):
                        for stmt in step.split(";"):
                            if stmt.strip():
                                self.con.execute(stmt)
                    else:
                        step(self.con)
                    self.con.execute("INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)", (ver, now))

    def warnings(self) -> list[dict]:
        return [dict(r) for r in self.con.execute("SELECT path, agent, reason, ts FROM ingest_warnings ORDER BY ts DESC")]
    def ingest_file(self, path, agent):
        p = Path(path)
        try:
            st_stat, p_key = p.stat(), str(p.resolve())
        except OSError:
            st_stat, p_key = None, str(path)
        if st_stat is not None:
            prev = self.con.execute("SELECT size, mtime_ns FROM ingested_files WHERE path = ? AND agent = ?", (p_key, agent)).fetchone()
            if prev and tuple(prev) == (st_stat.st_size, st_stat.st_mtime_ns):
                return 0
        warnings, excluded = [], _load_excluded_projects()
        raw_chunks, raw_usages = parse_transcript(path, agent, warnings=warnings)
        inserted, sessions = 0, {}
        sidechain = agent == "claude" and p.parent.name == "subagents"
        for record in raw_chunks + raw_usages:
            sessions.setdefault(record["session_id"], []).append(record)
        cwd_repos = {cwd: _repo_at(cwd) if cwd else None for cwd in {r["cwd"] for r in raw_chunks + raw_usages}}
        projects = {}
        for sid, records in sessions.items():
            if next((_one_line(_clean_user_prompt(c["text"])) for c in records if c.get("role") == "user"), "").casefold().startswith(_EVAL_PREFIXES):
                continue
            repo = next((cwd_repos[r["cwd"]] for r in records if cwd_repos[r["cwd"]]), None)
            if not repo:
                touched = sorted(((not r.get("changed"), (Path(r["cwd"] or "") / x).parent) for r in records for x in r.get("paths", ()) if r["cwd"] or Path(x).is_absolute()), key=lambda x: x[0])
                repo = next((found for d in dict.fromkeys(d for _, d in touched) if (found := _repo_at(d))), None)
            projects[sid] = (_project_scope(repo[1])[0], repo[0]) if repo else ("misc", None)
        with self.con:
            if st_stat is not None:
                self.con.execute("INSERT OR REPLACE INTO ingested_files (path, agent, size, mtime_ns) VALUES (?, ?, ?, ?)", (p_key, agent, st_stat.st_size, st_stat.st_mtime_ns))
            self.con.execute("DELETE FROM ingest_warnings WHERE path = ? AND agent = ?", (str(path), agent))
            now = datetime.now(timezone.utc).isoformat()
            self.con.executemany("INSERT OR REPLACE INTO ingest_warnings (path, agent, reason, ts) VALUES (?, ?, ?, ?)", [(str(path), agent, w, now) for w in warnings])
            prior = {}
            for sid, (project, _) in projects.items():
                row = self.con.execute("SELECT project FROM entries WHERE session_id = ? AND source_type != 'note' ORDER BY rowid LIMIT 1", (sid,)).fetchone()
                if row and row[0] != project:
                    if row[0] == "misc":
                        if project in excluded:
                            self.con.execute("DELETE FROM entries_fts WHERE id IN (SELECT id FROM entries WHERE project = 'misc' AND session_id = ? AND source_type != 'note')", (sid,))
                            self.con.execute("DELETE FROM entries WHERE project = 'misc' AND session_id = ? AND source_type != 'note'", (sid,))
                            self.con.execute("DELETE FROM usage WHERE project = 'misc' AND session_id = ?", (sid,))
                        self.con.execute("UPDATE entries SET project = ? WHERE project = 'misc' AND session_id = ? AND source_type != 'note'", (project, sid))
                        self.con.execute("UPDATE usage SET project = ? WHERE project = 'misc' AND session_id = ?", (project, sid))
                        self.con.execute("DELETE FROM entries_fts WHERE id IN (SELECT id FROM handoffs WHERE project = 'misc' AND session_id = ?)", (sid,))
                        self.con.execute("DELETE FROM handoffs WHERE project = 'misc' AND session_id = ?", (sid,))
                    else:
                        prior[sid] = row[0]  # Legacy scopes stay where they are.
            for c in raw_chunks:
                sid = c["session_id"]
                if sid not in projects:
                    continue
                project = prior.get(sid, projects[sid][0])
                if not c["text"] or project in excluded or (c["role"] == "tool" and c["text"].split(None, 1)[0] in ("TodoWrite", "Skill", "AskUserQuestion", "ListMcpResourcesTool", "SlashCommand")):
                    continue
                clean_text = mask_secrets(_clean_user_prompt(c["text"]) if c["role"] == "user" else c["text"])
                if not clean_text:
                    continue
                scope = _project_scope(project)
                pin = _in_sql("project", scope)
                for piece_i, piece in enumerate(split_text(clean_text, max_tokens=250)):
                    h = _nh(piece) if c["role"] != "user" and len(piece) >= 200 else None
                    if h and self.con.execute(f"SELECT 1 FROM entries WHERE {pin} AND text_hash = ? LIMIT 1", (*scope, h)).fetchone():
                        continue
                    piece_key = f"{c['dedupe_key']}:{piece_i}"
                    chunk_id = f"c_{hashlib.sha256(piece_key.encode()).hexdigest()[:12]}"
                    cur = self.con.execute(
                        "INSERT OR IGNORE INTO entries (id, project, text, source_type, session_id, ts, state, verified, stale_hint, dedupe_key, agent, model, text_hash) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'active', NULL, 0, ?, ?, ?, ?)",
                        (chunk_id, project, piece, c["role"], c["session_id"], c["ts"], piece_key, agent, c["model"], h)
                    )
                    if cur.rowcount > 0:
                        self.con.execute("INSERT INTO entries_fts (id, text_norm) VALUES (?, ?)", (chunk_id, _fts_norm(f"{agent or ''} {piece}".strip())))
                        inserted += 1
            _link_handoffs(self.con, [(c["session_id"], c["text"]) for c in raw_chunks if c["role"] == "tool" and c["session_id"] in projects])
            h_used = 0
            if raw_usages:
                h_used = int(any(has_session_start_ctx(c["text"], ("Handoffs:\n",)) for c in raw_chunks))
                if not h_used:
                    try:
                        with open(path, "r", encoding="utf-8", errors="replace") as f:
                            h_used = int(any(has_session_start_ctx(line, ("Handoffs:\\n",)) for line in f))  # raw JSON: escaped newline
                    except OSError:
                        pass
            for u in raw_usages:
                sid = u["session_id"]
                if sid not in projects:
                    continue
                project = prior.get(sid, projects[sid][0])
                if project in excluded:
                    continue
                self.con.execute(
                    "INSERT OR IGNORE INTO usage (session_id, agent, project, ts, uncached_input, cache_read, cache_write, output, memory_used, handoff_used, dedupe_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (u["session_id"], agent, project, u["ts"], u["uncached_input"], u["cache_read"],
                     u["cache_write"], u["output"], u["memory_used"], h_used, u["dedupe_key"])
                )
                if u["memory_used"]:
                    self.con.execute("UPDATE usage SET memory_used = 1 WHERE session_id = ? AND memory_used = 0", (u["session_id"],))
                if h_used:
                    self.con.execute("UPDATE usage SET handoff_used = 1 WHERE session_id = ? AND handoff_used = 0", (u["session_id"],))
            for sid, (project, root) in projects.items():
                project = prior.get(sid, project)
                if sid and not sidechain and project not in excluded:
                    self._upsert_handoff(project, sid, agent, sessions[sid], root)
        return inserted

    def _upsert_handoff(self, project, session_id, agent, records, root):
        asked, done, files, errors = "", "", [], ""
        for c in records:
            role = c.get("role")
            if role == "user" and not asked:
                asked = _one_line(_clean_user_prompt(c["text"]))
            elif role == "assistant":
                done = _one_line(c["text"]) or done
            elif role == "tool":
                errors = c.get("error") or errors
                if root and c["changed"]:
                    for path in c["paths"]:
                        full = (Path(c["cwd"] or root) / path).resolve()
                        rel = full.relative_to(root) if full.is_relative_to(root) else None
                        if rel and rel.parts and not {"tmp", "scratch", "scratch_build", "scratchpad"}.intersection(part.lower() for part in rel.parts):
                            files.append(rel.as_posix())
        if not asked and (not done or _EVAL_RESULT.search(done)):
            return
        shown_files = []
        for f in list(dict.fromkeys(reversed(files)))[:5]:
            if len(", ".join([*shown_files, f])) <= 115:
                shown_files.append(f)
        todo = self.con.execute("SELECT text FROM entries WHERE project = ? AND source_type = 'note' AND kind = 'todo' AND state = 'active' ORDER BY ts DESC LIMIT 1", (project,)).fetchone()
        text = _handoff_text(asked, done, ", ".join(shown_files), _one_line(errors), _one_line(todo[0]) if todo else "")
        ts = max((r["ts"] for r in records if r["ts"]), default=None) or datetime.now(timezone.utc).isoformat()
        nid = f"h_{hashlib.sha256(f'{project}:{session_id}'.encode()).hexdigest()[:12]}"
        self.con.execute(
            "INSERT INTO handoffs (id, project, text, session_id, ts, state, agent) VALUES (?, ?, ?, ?, ?, 'active', ?) "
            "ON CONFLICT(project, session_id) DO UPDATE SET text=excluded.text, ts=excluded.ts, agent=excluded.agent",
            (nid, project, text, session_id, ts, agent),
        )
        self.con.execute("DELETE FROM entries_fts WHERE id = ?", (nid,))
        self.con.execute("INSERT INTO entries_fts (id, text_norm) VALUES (?, ?)", (nid, _fts_norm(f"{KIND_LABELS['handoff']} {agent or ''} {text}".strip())))

    def save_note(self, project, text, kind, session_id=None, quote=None, replaces=None, files=None, repo=None, agent=None):
        clean_text = mask_secrets(text)
        clean_quote = mask_secrets(quote) if quote else None
        note_id = f"n_{uuid.uuid4().hex[:12]}"
        ts = datetime.now(timezone.utc).isoformat()
        norm_quote = " ".join(normalize(clean_quote).split()) if session_id and clean_quote else ""
        verified = bool(norm_quote) and norm_quote in " ".join(" ".join(normalize(c["text"]).split()) for c in self.con.execute(
            "SELECT text FROM entries WHERE session_id = ? AND source_type != 'note' ORDER BY rowid ASC", (session_id,)).fetchall())

        with self.con:
            if replaces:
                self.con.execute("UPDATE entries SET state = 'superseded' WHERE id = ? AND project = ?", (replaces, project))
                if dup := self.con.execute("SELECT id FROM entries WHERE project = ? AND source_type = 'note' AND state = 'active' AND kind = ? "
                                           "AND text = ? AND replaces IS NULL", (project, kind, clean_text)).fetchone():
                    self.con.execute("UPDATE entries SET replaces = ? WHERE id = ?", (replaces, dup["id"]))
                    return dup["id"]  # re-save after a replace hint: link the note saved a moment ago, no duplicate
            repo_str = str(repo) if repo else None
            files_json = json.dumps(files) if (files and repo_str) else None
            file_hashes_json = json.dumps({f: get_git_commit(repo_str, f) or "" for f in files}) if files_json else None
            self.con.execute(
                "INSERT INTO entries (id, project, text, source_type, session_id, ts, state, verified, stale_hint, kind, quote, replaces, files, repo, file_hashes, agent, model) "
                "VALUES (?, ?, ?, 'note', ?, ?, 'active', ?, 0, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (note_id, project, clean_text, session_id, ts, int(verified), kind, clean_quote, replaces, files_json, repo_str, file_hashes_json, agent),
            )
            prefix = f"{KIND_LABELS.get(kind, '')} {agent or ''}".strip()
            self.con.execute("INSERT INTO entries_fts (id, text_norm) VALUES (?, ?)", (note_id, _fts_norm(f"{prefix} {clean_text}".strip())))
            if kind == "handoff" and session_id:  # +21: the user turn in progress is counted only at its Stop
                self.con.execute("INSERT INTO stop_turns (session_id, next_block) VALUES (?, 21) ON CONFLICT(session_id) DO UPDATE SET next_block = turns + 21", (session_id,))
        return note_id

    def handoff_due(self, session_id: str) -> bool:
        """Count one finished user turn. True first at turn 8, then at most once per 20 turns."""
        with self.con:
            self.con.execute("INSERT INTO stop_turns (session_id, turns) VALUES (?, 1) ON CONFLICT(session_id) DO UPDATE SET turns = turns + 1", (session_id,))
            turns, next_block = self.con.execute("SELECT turns, next_block FROM stop_turns WHERE session_id = ?", (session_id,)).fetchone()
            if turns < next_block:
                return False
            self.con.execute("UPDATE stop_turns SET next_block = ? WHERE session_id = ?", (turns + 20, session_id))
            return True

    def replace_hints(self, project, text, kind) -> list[str]:
        """Active notes a new decision/fact may replace. Only a hint for the agent: never changes any state."""
        if kind not in ("decision", "fact") or not SUPERSEDE_CUE_RE.search(fold_tr(text)) or not (new := _sig_words(text)):
            return []
        rows = self.con.execute("SELECT id, text FROM entries WHERE project = ? AND source_type = 'note' AND state = 'active' "
                                "AND kind IN (?, 'todo') ORDER BY ts DESC", (project, kind)).fetchall()
        return [r["id"] for r in rows if (n := len(new & (old := _sig_words(r["text"])))) >= 2 and n * 2 >= min(len(new), len(old))]

    def get(self, id, project=None, before=0, after=0):
        row = self.con.execute(f"SELECT {ENTRY_COLS} FROM entries WHERE id = ?", (id,)).fetchone()
        row = row or self.con.execute(f"SELECT {HANDOFF_COLS} FROM handoffs WHERE id = ?", (id,)).fetchone()
        if not row or (project is not None and row["project"] not in _project_scope(project) + ("global",)):
            return None
        cache = {}
        out = _row_to_dict(row, cache)
        if before > 0 or after > 0:
            out["before"], out["after"] = [], []
            if row["source_type"] != "note" and row["session_id"]:
                sess = self.con.execute(
                    f"SELECT {ENTRY_COLS} FROM entries WHERE session_id = ? AND project = ? AND source_type != 'note' "
                    "ORDER BY COALESCE(ts, ''), rowid ASC", (row["session_id"], row["project"]),
                ).fetchall()
                idx = next((i for i, r in enumerate(sess) if r["id"] == id), None)
                if idx is not None:
                    out["before"] = [_row_to_dict(r, cache) for r in sess[max(0, idx - before):idx]] if before > 0 else []
                    out["after"] = [_row_to_dict(r, cache) for r in sess[idx + 1:idx + 1 + after]] if after > 0 else []
        return out

    def _with_prev_assistant(self, r, cap_tokens: int) -> str:
        txt = r["text"]
        max_add = min(240, cap_tokens * 4 - len(txt))
        if max_add < 32 or r["source_type"] != "tool" or not r["session_id"] or r["rid"] is None:
            return txt
        prev = self.con.execute(
            "SELECT text FROM entries WHERE session_id = ? AND project = ? AND source_type = 'assistant' "
            "AND (COALESCE(ts, ''), rowid) < (?, ?) ORDER BY COALESCE(ts, '') DESC, rowid DESC LIMIT 1",
            (r["session_id"], r["project"], r["ts"] or "", r["rid"]),
        ).fetchone()
        if not prev or not (p := " ".join(prev["text"].split())):
            return txt
        hdr = "\n[prev assistant: "
        return f"{txt}{hdr}{_clip(p, max_add - len(hdr) - 1)}]"

    def _agent_handoffs(self, scope) -> dict:
        """session_id (or note id, if it has no session) -> newest active agent-written handoff."""
        return {r["session_id"] or r["id"]: r for r in self.con.execute(f"SELECT id, session_id, ts, text, agent FROM entries WHERE {_in_sql('project', scope)} AND source_type = 'note' "
                                                             "AND kind = 'handoff' AND state = 'active' ORDER BY ts, rowid", scope)}

    def _session_handoffs(self, scope, agent_ho, limit) -> list:
        cpu = self.con.execute(f"SELECT session_id, ts, text, agent FROM handoffs WHERE {_in_sql('project', scope)} AND state = 'active'", scope)
        rows = [*agent_ho.values(), *(h for h in cpu if h["session_id"] not in agent_ho)]
        rows.sort(key=lambda h: _parse_ts(h["ts"]) or 0.0, reverse=True)
        return [{**h, "text": text} for h in rows if (text := _shown_handoff(h["text"], h["agent"]))][:limit]

    def search(self, query, project, budget_tokens=800, kind=None):
        queries = [q for q in (query if isinstance(query, (list, tuple)) else [query]) if isinstance(q, str) and q.strip()]
        if not queries:
            return {"status": "no_match", "results": [], "tokens": 0}
        scope_g = _project_scope(project) + ("global",)
        epin, hpin, pin = _in_sql("e.project", scope_g), _in_sql("handoffs.project", scope_g), _in_sql("project", scope_g)
        total_docs = max(self.con.execute("SELECT count(*) FROM entries_fts_docsize").fetchone()[0], 1)  # one row per FTS row, no text read
        df_cache, stale_cache, agent_ho = {}, {}, self._agent_handoffs(scope_g)  # per call: query variants repeat terms; notes repeat (repo, file)

        def doc_count(sql, args):
            if (sql, args) not in df_cache:
                df_cache[(sql, args)] = self.con.execute(sql, args).fetchone()[0]
            return df_cache[(sql, args)]

        candidate_map, scope_map, doc_cache = {}, None, {}
        for q_str in queries:
            q_norm = normalize(q_str)
            groups = [[w for w in re.findall(r"\w+", t) if w not in STOP_WORDS and not any(s in TR_STOP_WORDS for s in tr_stem(w))]
                      for t in re.findall(r"\w+(?:[./]\w+)*", q_norm)]
            meaningful = [w for g in groups for w in g]
            if not meaningful:
                continue
            word_div = {w: len(g) for g in groups for w in g}
            exact_map = {w: tr_stem(w) for w in meaningful}
            fold_map = {w: list(dict.fromkeys([fold_tr(s) for s in exact_map[w]] + tr_stem(fold_tr(w)))) for w in meaningful}

            weights, fts_parts = {}, []
            for w in meaningful:
                all_stems = list(dict.fromkeys(exact_map[w] + fold_map[w]))
                valid_stems = [s for s in all_stems if len(s) >= 3]
                min_stems = [s for s in valid_stems if not any(o != s and o in s for o in valid_stems)]
                if min_stems:
                    fts_term = f'"{min_stems[0]}"' if len(min_stems) == 1 else "(" + " OR ".join(f'"{s}"' for s in min_stems) + ")"
                    fts_parts.append(fts_term)
                    cnt = doc_count("SELECT count(*) FROM entries_fts WHERE entries_fts MATCH ?", (fts_term,))
                else:
                    fw = fold_tr(w)
                    like_args = (f"%{w}%", f"%{fw}%") if fw != w else (f"%{w}%",)
                    cnt = doc_count(f"SELECT count(*) FROM entries_fts_content WHERE {' OR '.join('c1 LIKE ?' for _ in like_args)}", like_args)  # c1 = text_norm
                weights[w] = math.log((total_docs - cnt + 0.5) / (cnt + 0.5) + 1.0) / word_div[w]
            total_weight = sum(weights.values())

            ecols = ", ".join(f"e.{c.strip()}" for c in f"{ENTRY_COLS}, replaces".split(",")) + ", e.rowid AS rid"
            h_sel = f"SELECT {HANDOFF_COLS}, NULL AS replaces, NULL AS rid"
            if fts_parts:
                if scope_map is None:  # per call: FTS rowid -> (stage, id); filtering on it first keeps bm25 off other scopes' rows
                    fc = "JOIN entries_fts_content fc ON fc.c0"  # idx_fts_content_id: FTS rowid without reading its text
                    found = self.con.execute(f"SELECT fc.id, e.source_type = 'tool', fc.c0 FROM entries e {fc} = e.id WHERE {epin} AND (? IS NULL OR e.kind = ?)", (*scope_g, kind, kind)).fetchall()
                    if kind in (None, "handoff"):
                        found += self.con.execute(f"SELECT fc.id, 2, fc.c0 FROM handoffs {fc} = handoffs.id WHERE {hpin}", scope_g).fetchall()
                    scope_map = {x[0]: x[2] for x in found}
                    self.con.create_function("lore_in_scope", 1, {x[0]: x[1] for x in found}.get)
                top = ({}, {}, {})  # top 200 by bm25 per stage: non-tool, tool, handoff
                for fr, bm, st in self.con.execute(
                        "SELECT r, bm, st FROM (SELECT r, bm, st, ROW_NUMBER() OVER (PARTITION BY st ORDER BY bm, r) AS n FROM (SELECT rowid AS r, bm25(entries_fts) AS bm, "
                        "lore_in_scope(rowid) AS st FROM entries_fts WHERE entries_fts MATCH ? AND lore_in_scope(rowid) IS NOT NULL)) WHERE n <= 200 ORDER BY st, n", (" OR ".join(fts_parts),)):
                    top[st][scope_map[fr]] = bm
                rows = []
                for ids, sel in zip(top, (f"SELECT {ecols} FROM entries e WHERE e.id", f"SELECT {ecols} FROM entries e WHERE e.id", f"{h_sel} FROM handoffs WHERE handoffs.id")):
                    by_id = {x["id"]: x for x in self.con.execute(f"{sel} IN ({','.join('?' * len(ids))})", tuple(ids))} if ids else {}
                    rows += [(by_id[i], bm) for i, bm in ids.items() if i in by_id]  # a concurrent purge may have removed it
            else:  # no stem of 3+ chars: LIKE scan, first 200 per stage
                like_terms = list(dict.fromkeys(t for w in meaningful for t in (w, fold_tr(w))))
                m_cond, m_args = "(" + " OR ".join("f.text_norm LIKE ?" for _ in like_terms) + ")", [f"%{t}%" for t in like_terms]
                rows = []
                for s_flt in ("e.source_type != 'tool'", "e.source_type = 'tool'") if kind is None else ("e.source_type != 'tool'",):
                    rows += [(x, 0.0) for x in self.con.execute(
                        f"SELECT {ecols} FROM entries_fts f JOIN entries e ON f.id = e.id WHERE {m_cond} AND {epin} AND (? IS NULL OR e.kind = ?) AND {s_flt} LIMIT 200",
                        m_args + [*scope_g, kind, kind])]
                if kind in (None, "handoff"):
                    rows += [(x, 0.0) for x in self.con.execute(f"{h_sel} FROM entries_fts f JOIN handoffs ON f.id = handoffs.id WHERE {m_cond} AND {hpin} LIMIT 200", m_args + [*scope_g])]

            fstarts = {fold_tr(s)[:3] for stems in fold_map.values() for s in stems}
            for r, bm in rows:
                if r["kind"] == "handoff" and (ah := agent_ho.get(r["session_id"])) and ah["id"] != r["id"]:
                    continue  # the session's newest agent handoff replaces its CPU and older ones
                label = KIND_LABELS.get(r["kind"], "") if r["source_type"] == "note" else ""
                if (doc := doc_cache.get(r["id"])) is None:  # rows repeat across query variants
                    text_norm = normalize(" ".join(p for p in (label, r["agent"] or "", r["text"]) if p))
                    doc = doc_cache[r["id"]] = (text_norm, text_norm if text_norm.isascii() else text_norm.translate(FOLD), set(re.findall(r"\w+", text_norm)),
                                               "_" in r["text"] and bool(re.search(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", r["text"])))
                text_norm, text_fold, doc_words, has_const = doc
                cand_words = [dw for dw in doc_words if (p3 := dw[:3]) in fstarts or (not p3.isascii() and fold_tr(p3) in fstarts)]
                doc_stems = set().union(*(tr_stem(dw) for dw in cand_words)) if cand_words else set()
                folded_only = [fw for dw in cand_words if (fw := fold_tr(dw)) != dw]
                doc_fold_stems = doc_stems | {fold_tr(s) for s in doc_stems} | (set().union(*(tr_stem(fw) for fw in folded_only)) if folded_only else set())
                exact_w, match_w = 0.0, 0.0
                for mw in meaningful:
                    ex = any(s in doc_stems or (len(s) >= 4 and s in text_norm) for s in exact_map[mw])
                    fl = ex or any(s in doc_fold_stems or (len(s) >= 4 and s in text_fold) for s in fold_map[mw])
                    exact_w += weights[mw] if ex else 0.0
                    match_w += weights[mw] if fl else 0.0
                weighted_share = match_w / total_weight if total_weight > 0 else 0.0
                if weighted_share < 0.50:
                    continue
                exact_share = exact_w / total_weight if total_weight > 0 else 0.0
                score = weighted_share * 10.0 + exact_share * 1.0 + min(max(-bm, 0.0), 3.0)
                score += 2.0 if r["state"] == "active" else -0.5
                if r["source_type"] == "note":
                    score += 1.0 if r["kind"] == "handoff" else 3.0
                elif r["source_type"] in ("assistant", "user"):
                    score += 2.5 if r["source_type"] == "assistant" else 1.5
                if has_const:
                    score += 0.2
                if r["id"] not in candidate_map or score > candidate_map[r["id"]][0]:
                    candidate_map[r["id"]] = (score, r)

        if not candidate_map:
            return {"status": "no_match", "results": [], "tokens": 0}

        parsed = [(_parse_ts(r["ts"]), score, r) for score, r in candidate_map.values()]
        newest_ts = max((t for t, _, _ in parsed if t is not None), default=0.0)
        candidates = [(score + (0.6 / (1.0 + max((newest_ts - t) / 86400.0, 0.0) / 14.0) if t is not None else 0.0), t or 0.0, r)
                      for t, score, r in parsed]
        candidates.sort(key=lambda x: (-x[0], -x[1]))

        results, seen_ids, sess_counts, cur_tokens, tool_tokens = [], set(), {}, 0, 0
        tool_cap = int(budget_tokens * 0.4)
        best_score = candidates[0][0]
        for item_score, _t, r in candidates:
            if r["id"] in seen_ids or (best_score > 0 and item_score < best_score * 0.5):
                continue
            sid = r["session_id"] if r["source_type"] != "note" else None
            if sid and sess_counts.get(sid, 0) >= 2:
                continue
            to_add = [r]
            if r["source_type"] == "note":
                p_sql = f"SELECT {ENTRY_COLS}, replaces, rowid AS rid FROM entries WHERE {{cond}} AND {pin} AND (? IS NULL OR kind = ?)"
                if p_by := self.con.execute(p_sql.format(cond="replaces = ?"), (r["id"], *scope_g, kind, kind)).fetchone():
                    to_add.append(p_by)
                if r["replaces"] and (p_old := self.con.execute(p_sql.format(cond="id = ?"), (r["replaces"], *scope_g, kind, kind)).fetchone()):
                    to_add.append(p_old)
            for idx_add, row_add in enumerate(to_add):
                if row_add["id"] in seen_ids:
                    continue
                is_tool = row_add["source_type"] == "tool"
                avail = min(budget_tokens - cur_tokens, tool_cap - tool_tokens) if is_tool else budget_tokens - cur_tokens
                if math.ceil(len(row_add["text"]) / 4) > avail:
                    if idx_add == 0:
                        break
                    continue
                item = _row_to_dict(row_add, stale_cache)
                item["text"] = self._with_prev_assistant(row_add, avail)
                tc = math.ceil(len(item["text"]) / 4)
                results.append(item)
                seen_ids.add(row_add["id"])
                cur_tokens += tc
                tool_tokens += tc if is_tool else 0
                if idx_add == 0 and sid:
                    sess_counts[sid] = sess_counts.get(sid, 0) + 1
        if not results:
            for item_score, _t, r in candidates:
                if (best_score <= 0 or item_score >= best_score * 0.5) and math.ceil(len(r["text"]) / 4) <= budget_tokens:
                    item = _row_to_dict(r, stale_cache)
                    item["text"] = self._with_prev_assistant(r, budget_tokens)
                    results.append(item)
                    cur_tokens = math.ceil(len(item["text"]) / 4)
                    break

        if not results:
            return {"status": "no_match", "results": [], "tokens": 0}
        return {"status": "ok", "results": results, "tokens": cur_tokens}

    def index(self, project, budget_tokens=400):
        scope = _project_scope(project)
        scope_g = scope + ("global",)
        pin_g = _in_sql("project", scope_g)
        counts = dict(self.con.execute("SELECT project, count(*) FROM entries GROUP BY project"))
        allowed = [f"global ({counts['global']})"] if counts.get("global") else []
        if count := sum(counts.get(p, 0) for p in scope if p != "global"):
            allowed.append(f"{scope[0]} ({count})")
        outside = counts.keys() - set(scope_g)
        research = sum(p.startswith("research:") for p in outside)
        allowed += [f"+{n} {label}" for n, label in ((len(outside) - research, "other projects"), (research, "research scopes")) if n]
        scopes_line = "Scopes: " + ", ".join(allowed) if allowed else ""

        notes = self.con.execute(f"""
            SELECT kind, text, ts FROM entries
            WHERE {pin_g} AND source_type = 'note' AND state = 'active' AND COALESCE(kind, '') != 'handoff'
            ORDER BY CASE WHEN kind = 'decision' THEN 0 ELSE 1 END, ts DESC, id DESC
        """, scope_g).fetchall()
        agent_ho = self._agent_handoffs(scope)
        handoffs = self._session_handoffs(scope, agent_ho, 3)
        if not scopes_line and not notes and not handoffs:
            return ""
        lines = [scopes_line] if scopes_line else []
        for n in notes[:3]:
            txt = _clip(" ".join(n["text"].split()), 88)
            lines.append(f"- [{n['kind']}] {txt}")
        if handoffs:
            lines.append("Handoffs:")
        for h in handoffs:
            lns = (h["text"] or "").splitlines()
            done = next((ln[6:] for ln in lns if ln.startswith("Done: ")), "") if h.get("session_id") not in agent_ho else ""
            title = done if len(done) > 15 else (lns[0].removeprefix("Asked: ") if lns else "")
            title = _clip(re.sub(r"^(?:[#>\-]+|\d+[.)])\s*|\*\*|`", "", title).strip(), 68)  # markdown marks waste startup tokens
            lines.append(f"- {(h['ts'] or '')[:10]}: {title}")
        if handoffs and handoffs[0]["session_id"] not in agent_ho:
            latest = next((ln[6:] for ln in handoffs[0]["text"].splitlines() if ln.startswith("Done: ")), "")
            if latest:
                lines.append(f"Last: {_clip(latest, 64)}")
        lines.append(f"{f'{len(notes)} notes. ' if notes else ''}Use memory_search / memory_save.")
        cap = min(budget_tokens, 181)
        kept = []
        for ln in lines:
            if math.ceil(len("\n".join(kept + [ln])) / 4) <= cap:
                kept.append(ln)
        return "\n".join(kept)

    def delete(self, id: str) -> bool:
        with self.con:
            res = self.con.execute("DELETE FROM entries WHERE id = ?", (id,))
            self.con.execute("DELETE FROM entries_fts WHERE id = ?", (id,))
            hres = self.con.execute("DELETE FROM handoffs WHERE id = ?", (id,))
            return res.rowcount > 0 or hres.rowcount > 0

    def purge(self, project: str) -> int:
        scope = _project_scope(project)
        pin = _in_sql("project", scope)
        with self.con:
            self.con.execute(f"DELETE FROM entries_fts WHERE id IN (SELECT id FROM entries WHERE {pin}) OR id IN (SELECT id FROM handoffs WHERE {pin})", scope + scope)
            res = self.con.execute(f"DELETE FROM entries WHERE {pin}", scope)
            hres = self.con.execute(f"DELETE FROM handoffs WHERE {pin}", scope)
            self.con.execute(f"DELETE FROM usage WHERE {pin}", scope)
            if res.rowcount > 0 or hres.rowcount > 0:
                self.con.execute("DELETE FROM ingested_files")
            return res.rowcount

    def purge_older_than(self, days: int, project: str | None = None) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        scope = _project_scope(project) if project else ()
        cond, prms = (f" WHERE {_in_sql('project', scope)}", scope) if scope else ("", ())
        old_ids = lambda tbl, col="id": [r[0] for r in self.con.execute(f"SELECT {col}, ts FROM {tbl}{cond}", prms).fetchall() if (t := _parse_ts(r[1])) is not None and t < cutoff]
        eids, hids, uids = old_ids("entries"), old_ids("handoffs"), old_ids("usage", "rowid")
        with self.con:
            for tbl, col, vals in (("entries_fts", "id", eids + hids), ("entries", "id", eids), ("handoffs", "id", hids), ("usage", "rowid", uids)):
                for i in range(0, len(vals), 500):
                    self.con.execute(f"DELETE FROM {tbl} WHERE {col} IN ({','.join('?' * len(vals[i:i + 500]))})", vals[i:i + 500])
        return len(eids)

    def list_entries(self, project: str | None = None) -> list[dict]:
        scope_g = _project_scope(project) + ("global",) if project else ()
        cond, prms = (f" WHERE {_in_sql('project', scope_g)}", scope_g) if scope_g else ("", ())
        sql = f"SELECT {ENTRY_COLS} FROM entries{cond} UNION ALL SELECT {HANDOFF_COLS} FROM handoffs{cond} ORDER BY ts DESC"
        cache = {}
        return [_row_to_dict(r, cache) for r in self.con.execute(sql, prms + prms).fetchall()]

    def sections(self, project, sessions=30) -> dict:
        """Project content shared by export and the CLI view: notes oldest first, sessions newest first."""
        scope = _project_scope(project)
        notes = self.con.execute(f"SELECT id, text, kind, state, agent, ts FROM entries WHERE {_in_sql('project', scope)} AND source_type = 'note' "
                                 "ORDER BY ts ASC, id ASC", scope).fetchall()
        layout = (("Open items", "active", "todo"), ("Decisions", "active", "decision"), ("Sessions", "", ""),
                  ("Failed Attempts", "active", "failed_attempt"), ("Facts", "active", "fact"), ("History", "superseded", None))
        out = {title: [n for n in notes if n["state"] == state and (kind is None or n["kind"] == kind)] for title, state, kind in layout}
        asked = {r[0]: _fields(_shown_handoff(r[1], r[2])).get("Asked", "") for r in self.con.execute(f"SELECT session_id, text, agent FROM handoffs WHERE {_in_sql('project', scope)}", scope)}
        out["Sessions"] = [{**h, "asked": asked.get(h["session_id"], "")} for h in self._session_handoffs(scope, self._agent_handoffs(scope), sessions)]
        return out

    def find_notes(self, project, ref) -> list:
        """Notes visible from project whose full id, or 8-char short id as shown by `view`, is ref."""
        scope = _project_scope(project) + ("global",)
        return self.con.execute(f"SELECT id, project, kind, state, text FROM entries WHERE {_in_sql('project', scope)} AND source_type = 'note' AND (id = ? OR substr(id, 1, 8) = ?)", (*scope, ref, ref)).fetchall()

    def scopes(self) -> list[tuple]:
        """[(scope, last activity)] newest first, aliases merged."""
        last = {}
        for p, ts in self.con.execute("SELECT project, MAX(ts) FROM entries GROUP BY project UNION ALL SELECT project, MAX(ts) FROM handoffs GROUP BY project"):
            last[name] = max(last.get(name := _project_scope(p)[0], 0.0), _parse_ts(ts) or 0.0)
        return sorted(last.items(), key=lambda x: (-x[1], x[0]))

    def latest_handoff(self, project) -> dict | None:
        return next(iter(self._session_handoffs(s := _project_scope(project), self._agent_handoffs(s), 1)), None)

    def export(self, project):
        out = [f"# {_project_scope(project)[0]}"]
        for title, items in self.sections(project).items():
            if title == "Sessions":
                out.append("\n## Sessions")
                day = None
                for h in items:
                    if (date := h["ts"][:10] if h["ts"] else "Unknown date") != day:
                        day = date
                        out.append(f"\n### {day}")
                    out.extend(("", f"**{h['agent'] or 'unknown'}**", h["text"]))
            elif items or title in ("Open items", "Decisions"):
                out.append(f"\n## {title}")
                out.extend("- {}{}".format("[{}] ".format(it["agent"]) if it["agent"] else "", " ".join(mask_secrets(it["text"]).splitlines())) for it in items)
        return "\n".join(out) + "\n"

    def stats(self, project: str | None = None, days: int | None = None, agent: str | None = None) -> dict:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() if days is not None else None
        scope = _project_scope(project) if project else ()
        pcond = _in_sql("project", scope) if scope else "1=1"
        rows = self.con.execute(
            f"SELECT session_id, agent, project, ts, uncached_input, cache_read, cache_write, output, memory_used, handoff_used "
            f"FROM usage WHERE {pcond} AND (? IS NULL OR agent = ?) ORDER BY agent, session_id, ts ASC, rowid ASC",
            scope + (agent, agent),
        ).fetchall()
        sessions = {}
        for r in rows:
            t_sec = _parse_ts(r["ts"])
            if cutoff is None or (t_sec is not None and t_sec >= cutoff):
                sessions.setdefault((r["agent"], r["session_id"]), []).append((t_sec, r))
        by_agent, add = {}, lambda d, **kw: d.update({k: d[k] + v for k, v in kw.items()})
        comp = {k: dict(sess=0, turns=0, turn_cost=0, starts=0, start_cost=0, f10=0, resumes=0, resume_cost=0) for k in ("with_memory", "without_memory", "with_handoff")}
        for (ag_name, _sid), turns in sessions.items():
            ag = by_agent.setdefault(ag_name, dict(sessions=0, turns=0, uncached_input=0, cache_read=0, cache_write=0, output=0, resume_turns=0, resume_cost=0))
            ag["sessions"] += 1
            buckets = [comp["with_memory" if any(r["memory_used"] for _, r in turns) else "without_memory"]]
            if any(r["handoff_used"] for _, r in turns):
                buckets.append(comp["with_handoff"])
            for b in buckets:
                b["sess"] += 1
            prev_t = None
            for idx, (t_sec, r) in enumerate(turns):
                cost = r["uncached_input"] + r["cache_write"]
                resumed = idx > 0 and prev_t is not None and t_sec is not None and t_sec - prev_t > 3600
                add(ag, turns=1, resume_turns=int(resumed), resume_cost=cost * resumed, **{k: r[k] for k in ("uncached_input", "cache_read", "cache_write", "output")})
                for b in buckets:
                    add(b, turns=1, turn_cost=cost, f10=cost if idx < 10 else 0, starts=int(idx == 0), start_cost=cost if idx == 0 else 0, resumes=int(resumed), resume_cost=cost * resumed)
                prev_t = t_sec if t_sec is not None else prev_t
        agents_out = [{
            "agent": ag, **{k: v for k, v in a.items() if k != "resume_cost"},
            "avg_tokens_per_turn": (a["uncached_input"] + a["cache_read"] + a["cache_write"] + a["output"]) // a["turns"] if a["turns"] else 0,
            "avg_resume_cost": a["resume_cost"] // a["resume_turns"] if a["resume_turns"] else 0,
        } for ag, a in sorted(by_agent.items())]
        comp_out = {k: {
            "sessions": cb["sess"], "turns": cb["turns"],
            "avg_turn_cost": cb["turn_cost"] // cb["turns"] if cb["turns"] else 0,
            "avg_session_start_cost": cb["start_cost"] // cb["starts"] if cb["starts"] else 0,
            "avg_first_10_cost": cb["f10"] // cb["sess"] if cb["sess"] else 0,
            "avg_resume_cost": cb["resume_cost"] // cb["resumes"] if cb["resumes"] else 0,
        } for k, cb in comp.items()}
        note_rows = self.con.execute(f"SELECT ts FROM entries WHERE {pcond} AND (? IS NULL OR agent = ?) AND source_type = 'note'", scope + (agent, agent),).fetchall()
        notes_cnt = sum(1 for r in note_rows if cutoff is None or ((t := _parse_ts(r["ts"])) is not None and t >= cutoff))
        tot_sess = sum(a["sessions"] for a in agents_out)
        return {"agents": agents_out, "memory_comparison": comp_out, "notes_count": notes_cnt, "notes_per_session": round(notes_cnt / tot_sess, 2) if tot_sess else 0.0}

    def close(self):
        if self.con is not None:
            self.con.close()
            self.con = None
