"""Login accounts. Passwords are stored ONLY as salted hashes in data/credentials.json
(kept out of git). Usernames are  firstname.lastname@merh.example.

    python -m app.users_admin init                  create accounts for every employee with a random
                                                    password (shown ONCE); refuses to overwrite
    python -m app.users_admin init --force          start over (all passwords change)
    python -m app.users_admin set-password E001     choose a password (typed hidden, asked twice)
    python -m app.users_admin list                  usernames and ids (never passwords or hashes)
"""
import argparse
import getpass
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

from werkzeug.security import generate_password_hash

from app.identity import all_records

DOMAIN = "merh.example"
MIN_PASSWORD = 10
_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"      # no look-alike characters


def credentials_path() -> Path:
    return Path(os.getenv("CREDENTIALS_FILE") or Path(__file__).resolve().parent.parent / "data" / "credentials.json")


def username_for(name: str) -> str:
    return name.strip().lower().replace(" ", ".") + "@" + DOMAIN


def generate_password() -> str:
    """12 random characters in three groups, e.g. 'hK7m-Qx3p-Wn9d' (about 69 bits)."""
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3))


def build_credentials(records, hasher=generate_password_hash):
    creds, shown = {}, {}
    for r in records:
        pw, user = generate_password(), username_for(r["name"])
        creds[user] = {"user_id": r["id"], "password_hash": hasher(pw)}
        shown[r["id"]] = (user, pw)
    return creds, shown


def save_credentials(creds: dict, path: Path | None = None) -> None:
    path = Path(path or credentials_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2)
    os.replace(tmp, path)                       # atomic: never leaves a half-written file


def load_credentials(path: Path | None = None) -> dict:
    path = Path(path or credentials_path())
    if not path.exists():
        raise FileNotFoundError(
            f"No login accounts found at {path}.\nCreate them with:  python -m app.users_admin init")
    creds = json.loads(path.read_text(encoding="utf-8"))
    for user, rec in creds.items():
        if not isinstance(rec, dict) or "user_id" not in rec or "password_hash" not in rec:
            raise ValueError(f"Malformed credentials entry for {user!r}")
    return creds


def set_password(creds: dict, user_id: str, password: str, hasher=generate_password_hash) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters.")
    for username, rec in creds.items():
        if rec["user_id"] == user_id:
            if password.lower() == username or password.lower() in username:
                raise ValueError("Password must not be part of the username.")
            rec["password_hash"] = hasher(password)
            return username
    raise ValueError(f"No account for {user_id!r}. Run 'init' first.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init")
    p_init.add_argument("--force", action="store_true")
    p_set = sub.add_parser("set-password")
    p_set.add_argument("user_id")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    path = credentials_path()

    if args.cmd == "init":
        if path.exists() and not args.force:
            print(f"{path} already exists. Use --force to replace it (every password will change).")
            return 1
        records = all_records()
        creds, shown = build_credentials(records)
        save_credentials(creds)
        print("Accounts created. These passwords are shown ONCE and are not stored anywhere readable.")
        print("Copy them now (or set your own later with: python -m app.users_admin set-password E001)\n")
        print(f'{"Name":<14}{"Role":<10}{"Username":<34}Password')
        for r in records:
            user, pw = shown[r["id"]]
            print(f'{r["name"]:<14}{r["role"]:<10}{user:<34}{pw}')
        return 0

    if args.cmd == "list":
        for user, rec in sorted(load_credentials(path).items(), key=lambda kv: kv[1]["user_id"]):
            print(f'{rec["user_id"]}  {user}')
        return 0

    creds = load_credentials(path)
    pw = getpass.getpass("New password: ")
    if pw != getpass.getpass("Repeat password: "):
        print("Passwords didn't match. Nothing changed.")
        return 1
    try:
        user = set_password(creds, args.user_id, pw)
    except ValueError as e:
        print(e)
        return 1
    save_credentials(creds)
    print(f"Password updated for {user}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
