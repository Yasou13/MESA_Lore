import asyncio
import json
import math
import os
import sys
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mesa_lore.mcp_server import TOOL_SPECS


def test_tool_specs():
    names = sorted(t["name"] for t in TOOL_SPECS)
    assert names == ["memory_get", "memory_index", "memory_save", "memory_search"]
    for t in TOOL_SPECS:
        assert "description" in t
        assert "inputSchema" in t
    size = math.ceil(len(json.dumps(TOOL_SPECS, ensure_ascii=False)) / 4)
    assert size <= 500


@pytest.mark.anyio
async def test_mcp_server_stdio(tmp_path):
    db_path = tmp_path / "mcp_test.db"
    env = os.environ.copy()
    env["MESA_LORE_DB"] = str(db_path)

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mesa_lore.mcp_server"],
        env=env,
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_list = await session.list_tools()
            tool_names = sorted(t.name for t in tool_list.tools)
            assert tool_names == ["memory_get", "memory_index", "memory_save", "memory_search"]

            # Save notes in demo, other, and global
            save_res = await session.call_tool(
                "memory_save",
                {"text": "Architecture decision: Use SQLite", "kind": "decision", "project": "demo"}
            )
            save_text = save_res.content[0].text
            assert "<untrusted_memory>" in save_text and "</untrusted_memory>" in save_text
            demo_id = save_text.split("Saved note: ")[1].split()[0]

            other_res = await session.call_tool(
                "memory_save",
                {"text": "Secret other project note", "kind": "fact", "project": "other-proj"}
            )
            other_id = other_res.content[0].text.split("Saved note: ")[1].split()[0]

            glob_res = await session.call_tool(
                "memory_save",
                {"text": "Global preference: concise answers", "kind": "fact", "project": "global"}
            )
            glob_id = glob_res.content[0].text.split("Saved note: ")[1].split()[0]

            # 1. memory_index (isolation + global visibility)
            idx_res = await session.call_tool("memory_index", {"project": "demo"})
            idx_text = idx_res.content[0].text
            assert "<untrusted_memory>" in idx_text and "</untrusted_memory>" in idx_text
            assert "Use SQLite" in idx_text
            assert "concise answers" in idx_text
            assert "Secret other project" not in idx_text

            # 2. memory_search (isolation + global visibility)
            search_res = await session.call_tool(
                "memory_search",
                {"query": "SQLite architecture", "project": "demo"}
            )
            search_text = search_res.content[0].text
            assert "<untrusted_memory>" in search_text and "</untrusted_memory>" in search_text
            assert "SQLite" in search_text

            glob_search = await session.call_tool(
                "memory_search",
                {"query": "concise answers", "project": "demo"}
            )
            assert "concise answers" in glob_search.content[0].text

            leak_search = await session.call_tool(
                "memory_search",
                {"query": "Secret other project", "project": "demo"}
            )
            assert "Secret other project" not in leak_search.content[0].text

            # 3. memory_get (valid, global, cross-project isolated, unknown id)
            get_ok = await session.call_tool("memory_get", {"id": demo_id, "project": "demo"})
            assert "<untrusted_memory>" in get_ok.content[0].text
            assert "Use SQLite" in get_ok.content[0].text

            get_glob = await session.call_tool("memory_get", {"id": glob_id, "project": "demo"})
            assert "<untrusted_memory>" in get_glob.content[0].text
            assert "concise answers" in get_glob.content[0].text

            get_other = await session.call_tool("memory_get", {"id": other_id, "project": "demo"})
            assert "<untrusted_memory>" in get_other.content[0].text
            assert "Secret other project" not in get_other.content[0].text
            assert "Item not found" in get_other.content[0].text

            get_unknown = await session.call_tool("memory_get", {"id": "nonexistent_id", "project": "demo"})
            assert "<untrusted_memory>" in get_unknown.content[0].text
            assert "Item not found" in get_unknown.content[0].text

            # 4. Bad arguments on tools must still wrap output in <untrusted_memory>
            bad_calls = [
                ("memory_save", {"text": "Bad kind note", "kind": "invalid_kind", "project": "demo"}),
                ("memory_save", {}),
                ("memory_search", {}),
                ("memory_get", {}),
            ]
            for tname, targs in bad_calls:
                bres = await session.call_tool(tname, targs)
                btext = bres.content[0].text
                assert "<untrusted_memory>" in btext and "</untrusted_memory>" in btext, (
                    f"{tname} with {targs} returned unwrapped text: {btext!r}"
                )
