# Changelog

## Round 4 (MVP Prep — `lore/round-4`)
- **Architecture & Resilience (U23–U26)**:
  - Extracted transcript parsers into `mesa_lore.readers.{claude,codex,antigravity}` behind a unified chunk/usage contract.
  - Added sequential non-destructive `schema_version` migrations (`v1`–`v5`) preserving Round 3 SQLite databases on open.
  - Added `ingest_warnings` format anomaly tracking surfaced via `mesa-lore doctor`.
  - Added `ingested_files` size/mtime tracking for O(1) incremental transcript skips and call-ID Antigravity tool pairing with FIFO fallback.
- **Recall Quality & Handoff (U27–U30)**:
  - Generated mechanical per-session `handoff` notes (`<= 400` tokens) in `handoffs` without LLM calls.
  - Added `before`/`after` chronological neighbor expansion to `Store.get`, `memory_get`, and `cli show`.
  - Capped search results at `2` chunks per session with source weighting (`manual notes > assistant > user > tool > handoff`).
  - Added rule-based `candidate` note extraction for explicit decision statements and `NOTE:`/`WHY:` lines.
- **Startup, Analytics, Tooling & CI (U31–U34, U36–U38)**:
  - Compressed `TOOL_SPECS` (`199` tok) and `Store.index` (`93` tok) under the `300`-token startup budget.
  - Extended `stats` with `--agent`, `First10` new-session cost, `With handoff` comparison row, and `Notes` per session.
  - Added `install`, `doctor`, and `uninstall` commands with pre-install backup manifest across Claude Code, Codex CLI (`timeout` key), Antigravity, and Claude Desktop (`--home` / `HOME`).
  - Added `purge --older-than <Nd>` and `~/.config/mesa-lore/config.toml` `excluded_projects` (Python 3.10+ compatible).
  - Added GitHub Actions CI matrix across Python `3.10`–`3.14` (`.github/workflows/ci.yml`), English `README.md`, `CHANGELOG.md`, and `LICENSE` placeholder.
  - **Skipped**: `U35` (`claude.ai` export reader) remains `waiting for export`.

## Round 3 (U16–U22)
- Added case/stem folding fixes, agent-name FTS indexing, lean search cutoffs, strict scope isolation in `index`, `TOOL_SPECS` alignment, multi-query search variants, Turkish consonant softening, tool input masking/trimming, Google Antigravity reader + auto-scan, and `mesa-lore stats` token telemetry.

## Round 2 (U10–U15)
- Added long-chunk splitting (`<= 250` tokens), note `kind` filtering, `global` & `research:<topic>` scopes, agent/model signature tags, and Markdown export (`mesa-lore export`).

## Round 1 (U1–U9)
- Initial SQLite FTS5 trigram store, Claude/Codex transcript ingest, secret masking, Turkish normalization/stemming, MCP stdio server (`memory_index`, `memory_search`, `memory_get`, `memory_save`), CLI & hooks (`SessionStart`, `Stop`, `SessionEnd`).
