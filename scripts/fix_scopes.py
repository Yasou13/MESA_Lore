#!/usr/bin/env python3
"""One-time repair: move transcript sessions of named junk scopes to misc.

Dry run by default (the database is opened read-only). --apply first writes and
verifies its own database backup. Notes never move. A session stays in its scope
when any of its working directories is still a git checkout.

Usage: python scripts/fix_scopes.py SCOPE [SCOPE ...] [--dry-run | --apply]
"""
import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from mesa_lore.readers import parse_transcript
from mesa_lore.store import _repo_at, get_db_path


def session_cwds(con):
    cwds = {}
    for path, agent in con.execute("SELECT path, agent FROM ingested_files").fetchall():
        try:
            for chunk in parse_transcript(path, agent)[0]:
                found = cwds.setdefault(chunk["session_id"], set())
                if chunk["cwd"]:
                    found.add(chunk["cwd"])
        except (OSError, ValueError):
            pass  # transcript gone: no git-root evidence
    return cwds


def find_moves(con, scopes, cwds):
    marks = ",".join("?" * len(scopes))
    rows = con.execute(f"SELECT session_id, project, count(*) FROM entries WHERE source_type != 'note' AND project IN ({marks}) GROUP BY 1, 2", scopes).fetchall()
    moves = [r for r in rows if not any(Path(d).is_dir() and _repo_at(d) for d in cwds.get(r[0], ()))]
    return rows, moves


def count_moved(con, moves):
    return sum(con.execute("SELECT count(*) FROM entries WHERE session_id = ? AND project = ? AND source_type != 'note'", (sid, project)).fetchone()[0]
               for sid, project, _ in moves)


def backup(con, db, moves):
    target = Path(f"{db}.before-fix-scopes-{datetime.now():%Y%m%dT%H%M%S}")
    if target.exists():
        raise SystemExit(f"backup already exists: {target}")
    con.execute("VACUUM INTO ?", (str(target),))
    copy = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True)
    try:
        if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or count_moved(copy, moves) != count_moved(con, moves):
            raise SystemExit(f"backup failed verification, nothing changed: {target}")
    finally:
        copy.close()
    return target


def apply_moves(con, moves):
    kept = 0
    with con:
        for sid, project, _ in moves:
            con.execute("UPDATE entries SET project = 'misc' WHERE session_id = ? AND project = ? AND source_type != 'note'", (sid, project))
            con.execute("UPDATE usage SET project = 'misc' WHERE session_id = ? AND project = ?", (sid, project))
            if con.execute("SELECT 1 FROM handoffs WHERE session_id = ? AND project = 'misc'", (sid,)).fetchone():
                kept += con.execute("SELECT count(*) FROM handoffs WHERE session_id = ? AND project = ?", (sid, project)).fetchone()[0]
            else:
                con.execute("UPDATE handoffs SET project = 'misc' WHERE session_id = ? AND project = ?", (sid, project))
    return kept


def main():
    parser = argparse.ArgumentParser(description="Move transcript sessions of named junk scopes to misc.")
    parser.add_argument("scopes", nargs="+", metavar="SCOPE")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show what would move (default)")
    mode.add_argument("--apply", action="store_true", help="back up the database, then move")
    args = parser.parse_args()
    if {"misc", "global"} & set(args.scopes):
        parser.error("misc and global cannot be repaired")
    db = get_db_path()
    if not db.is_file():
        raise SystemExit(f"database not found: {db}")
    con = sqlite3.connect(f"{db.resolve().as_uri()}?mode={'rw' if args.apply else 'ro'}", uri=True, timeout=30.0)
    try:
        cwds = session_cwds(con)
        rows, moves = find_moves(con, args.scopes, cwds)
        for sid, project, count in moves:
            print(f"{sid[:8]}  {project} -> misc  {count} entries  cwd={min(cwds.get(sid) or {'(no evidence)'})}")
        if not args.apply:
            print(f"dry run: {len(moves)} of {len(rows)} sessions would move")
        elif moves:
            print(f"backup: {backup(con, db, moves)}")
            kept = apply_moves(con, moves)
            print(f"moved {len(moves)} of {len(rows)} sessions; {kept} conflicting handoffs left in place")
        else:
            print(f"nothing to move in {len(rows)} sessions; database unchanged")
    finally:
        con.close()


if __name__ == "__main__":
    main()
