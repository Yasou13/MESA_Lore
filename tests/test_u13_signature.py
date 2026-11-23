import os
import sys
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import mcp.types as types
from mesa_lore.store import Store


@pytest.mark.anyio
async def test_mcp_client_signature(tmp_path):
    db_path = tmp_path / "sig_test.db"
    env = os.environ.copy()
    env["MESA_LORE_DB"] = str(db_path)

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mesa_lore.mcp_server"],
        env=env,
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read,
            write,
            client_info=types.Implementation(name="claude-code", version="1.0"),
        ) as session:
            await session.initialize()

            # Save note via MCP
            save_res = await session.call_tool(
                "memory_save",
                {
                    "text": "Migration from PostgreSQL to SQLite completed",
                    "kind": "decision",
                    "project": "proj-x",
                },
            )
            save_text = save_res.content[0].text
            assert "Saved note: " in save_text
            note_id = save_text.split("Saved note: ")[1].split()[0]

            # Verify in Store that agent is claude-code
            st = Store(db_path)
            note = st.get(note_id)
            assert note is not None
            assert note["agent"] == "claude-code"
            st.close()

            # Search via MCP and check tag format
            search_res = await session.call_tool(
                "memory_search",
                {"query": "PostgreSQL SQLite migration", "project": "proj-x"},
            )
            search_text = search_res.content[0].text
            assert note_id in search_text
            # Tag should have [claude-code·MM-DD]
            assert "[claude-code·" in search_text
