"""Tool authorisation tests (no LLM needed).

    python -m pytest tests/test_tools.py -q
"""
import asyncio
import os
import subprocess
import sys

import pytest

from app import tools
from app.identity import get_user

U = {i: get_user(i) for i in ("E001", "E002", "E003", "E004", "E005", "E006")}

# (asker, target, allowed?)   E002 manages E001+E006, E005 manages E004, E003 is HR
MATRIX = [
    ("E001", "E001", True), ("E001", "E002", False), ("E001", "E003", False),
    ("E001", "E004", False), ("E001", "E005", False), ("E001", "E006", False),
    ("E002", "E002", True), ("E002", "E001", True), ("E002", "E006", True),
    ("E002", "E004", False), ("E002", "E005", False), ("E002", "E003", False),
    ("E004", "E004", True), ("E004", "E005", False),          # nobody sees their own boss's balance
    ("E005", "E004", True), ("E005", "E002", False),
    ("E003", "E001", True), ("E003", "E002", True), ("E003", "E004", True),
    ("E003", "E005", True), ("E003", "E006", True),
]


@pytest.mark.parametrize("asker,target,allowed", MATRIX)
def test_balance_matrix_by_id(asker, target, allowed):
    out = tools.get_leave_balance(U[asker], target)
    assert ("error" not in out) == allowed, (asker, target, out)


def test_balance_by_full_and_first_name():
    assert tools.get_leave_balance(U["E002"], "Asha Rao")["id"] == "E001"
    assert tools.get_leave_balance(U["E002"], "asha")["id"] == "E001"
    assert "error" in tools.get_leave_balance(U["E001"], "Priya Nair")


def test_empty_target_means_myself():
    assert tools.get_leave_balance(U["E001"])["leave_balance_days"] == {"earned": 11, "casual": 5, "sick": 8}
    assert tools.get_leave_balance(U["E001"], "")["id"] == "E001"
    assert tools.get_leave_balance(U["E001"], "   ")["id"] == "E001"


def test_denied_and_nonexistent_look_identical():
    """The reply must not reveal which names are real."""
    real_but_denied = tools.get_leave_balance(U["E001"], "Ravi Menon")
    does_not_exist = tools.get_leave_balance(U["E001"], "Nobody Here")
    assert real_but_denied == does_not_exist == {"error": tools.NOT_FOUND}


def test_denied_reply_contains_no_balance_data():
    out = str(tools.get_leave_balance(U["E001"], "Ravi Menon"))
    assert "earned" not in out and "16" not in out


def test_profile_shows_manager():
    assert tools.get_my_profile(U["E001"])["manager"] == "Ravi Menon"
    assert tools.get_my_profile(U["E002"])["manager"] is None


def test_list_team():
    assert "error" in tools.list_team(U["E001"])
    assert {p["id"] for p in tools.list_team(U["E002"])["team"]} == {"E001", "E006"}
    assert {p["id"] for p in tools.list_team(U["E005"])["team"]} == {"E004"}
    assert {p["id"] for p in tools.list_team(U["E003"])["team"]} == {"E001", "E002", "E004", "E005", "E006"}


# ---- the MCP server itself (needs `pip install mcp`) ----

def _server_env(**extra):
    env = {k: v for k, v in os.environ.items() if k != "HR_USER_ID"}
    env.update(extra)
    return env


def test_server_refuses_to_start_without_identity():
    pytest.importorskip("mcp")
    p = subprocess.run([sys.executable, "-m", "app.mcp_server"], env=_server_env(),
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    assert p.returncode != 0


def test_server_refuses_unknown_identity():
    pytest.importorskip("mcp")
    p = subprocess.run([sys.executable, "-m", "app.mcp_server"], env=_server_env(HR_USER_ID="E999"),
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    assert p.returncode != 0


async def _call(user_id, tool, args=None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"],
                                   env=_server_env(HR_USER_ID=user_id), cwd=os.getcwd())
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            if tool == "__list__":
                return (await s.list_tools()).tools
            res = await s.call_tool(tool, args or {})
            return "".join(c.text for c in res.content if c.type == "text")


def test_mcp_identity_is_not_a_tool_argument():
    pytest.importorskip("mcp")
    for t in asyncio.run(_call("E001", "__list__")):
        assert set(t.inputSchema.get("properties", {})) <= {"employee"}, t.name


def test_mcp_enforces_access_end_to_end():
    pytest.importorskip("mcp")
    own = asyncio.run(_call("E001", "get_leave_balance", {}))
    assert '"earned": 11' in own
    other = asyncio.run(_call("E001", "get_leave_balance", {"employee": "E002"}))
    assert "error" in other and "16" not in other
    # the same call as E002's own report manager works: identity comes from the process
    assert '"earned": 11' in asyncio.run(_call("E002", "get_leave_balance", {"employee": "Asha Rao"}))
