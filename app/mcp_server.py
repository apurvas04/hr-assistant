"""MCP server exposing the personal-data tools.

Run as its own process, one per asking user:
    HR_USER_ID=E001 python -m app.mcp_server

The identity comes from the HR_USER_ID environment variable that the
orchestrator sets when it starts this process. It is NOT a tool argument, so the
model can never see it or change it. If it is missing or unknown the process
exits (fail closed).

IMPORTANT: this process talks to its client over stdout. Never print() here.
"""
import json
import os

from mcp.server.fastmcp import FastMCP

from app import tools
from app.identity import get_user

USER = get_user(os.environ["HR_USER_ID"])   # KeyError / PermissionError -> exit

mcp = FastMCP("hr-tools")


@mcp.tool()
def get_leave_balance(employee: str | None = None) -> str:
    """Get remaining leave days (earned, casual, sick) for an employee.
    Leave `employee` empty for the person asking. To look up someone else,
    pass their name or employee id; this only works for people the asker is
    allowed to view (their own reports for managers, anyone for HR)."""
    return json.dumps(tools.get_leave_balance(USER, employee))


@mcp.tool()
def get_my_profile() -> str:
    """Get the asking person's own profile: name, department, level and manager."""
    return json.dumps(tools.get_my_profile(USER))


@mcp.tool()
def list_team() -> str:
    """List the people the asker manages (their direct reports). HR gets everyone.
    Ordinary employees get an error."""
    return json.dumps(tools.list_team(USER))


if __name__ == "__main__":
    mcp.run()   # stdio transport
