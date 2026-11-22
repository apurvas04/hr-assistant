"""Who is asking, and what are they allowed to read.

In this project the user comes from data/policies/employees.json (a mock sign-in).
In a real deployment this would come from the gateway's SSO token. It must NEVER
come from the question text or from anything the model says.
"""
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

EMPLOYEES = Path("data/policies/employees.json")

# role -> which `access:` values in the policy front matter that role may read
ACCESS_BY_ROLE = {
    "employee": ("all",),
    "manager": ("all", "managers"),
    "hr": ("all", "managers", "hr"),
}


@dataclass(frozen=True)
class User:
    id: str
    name: str
    role: str
    department: str
    manager_id: str | None


@lru_cache(maxsize=1)
def _load() -> dict:
    return {e["id"]: e for e in json.loads(EMPLOYEES.read_text(encoding="utf-8"))}


def all_records() -> list[dict]:
    """Raw employee records (balances, level, manager). Only app/tools.py should call this."""
    return list(_load().values())


def get_user(employee_id: str) -> User:
    e = _load().get(employee_id)
    if e is None:
        raise PermissionError(f"unknown user: {employee_id!r}")
    return User(id=e["id"], name=e["name"], role=e["role"],
                department=e["department"], manager_id=e["manager_id"])


def allowed_access(user: User) -> tuple[str, ...]:
    """Fail closed: an unknown role gets an error, never a default."""
    try:
        return ACCESS_BY_ROLE[user.role]
    except KeyError:
        raise PermissionError(f"no access rules for role {user.role!r}") from None
