"""Personal-data tools. Pure functions, no MCP and no LLM, so they are easy to test.

Every tool receives the ASKING USER from the server, never from the model.
The model can choose WHICH person to ask about (the `employee` argument), but
this file decides whether the asker may see that person.

Rule (can_view): yourself; a manager sees their direct reports; HR sees everyone.
"""
from app.identity import User, all_records

# One message for "does not exist" and "not allowed to see", so the answer
# does not reveal which names are real.
NOT_FOUND = "No employee found that you are allowed to view."


def can_view(user: User, target: dict) -> bool:
    return (
        target["id"] == user.id
        or user.role == "hr"
        or (user.role == "manager" and target["manager_id"] == user.id)
    )


def _self(user: User) -> dict:
    return next(r for r in all_records() if r["id"] == user.id)


def _resolve(user: User, employee: str | None) -> dict | None:
    """Find the person among those the asker may view. None = not found OR not allowed."""
    if not employee or not employee.strip():
        return _self(user)
    needle = employee.strip().lower()
    matches = [
        r for r in all_records()
        if can_view(user, r)
        and needle in (r["id"].lower(), r["name"].lower(), r["name"].split()[0].lower())
    ]
    return matches[0] if len(matches) == 1 else None


def get_leave_balance(user: User, employee: str | None = None) -> dict:
    rec = _resolve(user, employee)
    if rec is None:
        return {"error": NOT_FOUND}
    return {"employee": rec["name"], "id": rec["id"], "leave_balance_days": rec["leave_balance"]}


def get_my_profile(user: User) -> dict:
    me = _self(user)
    boss = next((r for r in all_records() if r["id"] == me["manager_id"]), None)
    return {
        "name": me["name"], "id": me["id"], "role": me["role"],
        "department": me["department"], "level": me["level"],
        "manager": boss["name"] if boss else None,
    }


def list_team(user: User) -> dict:
    if user.role == "employee":
        return {"error": "Only managers and HR can list a team."}
    team = [r for r in all_records() if r["id"] != user.id and can_view(user, r)]
    return {"team": [{"id": r["id"], "name": r["name"], "department": r["department"],
                      "level": r["level"]} for r in team]}
