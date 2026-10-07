"""Exercise the real stdio protocol with an isolated profile and no portal access."""

import json
import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_server_exposes_tools_without_credentials(tmp_path):
    async def run():
        env = {**os.environ, "LOCALAPPDATA": str(tmp_path), "PYTHONUTF8": "1"}
        server = StdioServerParameters(command=sys.executable, args=["-m", "omgtu_student", "serve"], env=env)
        async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            assert len(tools) == 7
            assert "omgtu_latest_assignment" in tools
            assert all("password" not in json.dumps(tool.inputSchema) for tool in tools.values())
            assert tools["omgtu_list_courses"].annotations.readOnlyHint
            assert not tools["omgtu_bind_project"].annotations.readOnlyHint
            result = await session.call_tool("omgtu_auth_status", {})
            payload = result.structuredContent or json.loads(result.content[0].text)
            assert payload == {"status": "login_required", "authenticated": False}

    anyio.run(run)
