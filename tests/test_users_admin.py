"""Account-tool tests (no services):  python -m pytest tests/test_users_admin.py -q"""
import json
import os
import tempfile
from pathlib import Path

import pytest
from werkzeug.security import check_password_hash

from app import users_admin as ua
from app.identity import all_records

FAST = lambda pw: __import__("werkzeug.security", fromlist=["x"]).generate_password_hash(pw, method="pbkdf2:sha256:1000")


def _tmp():
    return Path(tempfile.mkdtemp()) / "credentials.json"


def test_usernames_follow_firstname_dot_lastname():
    assert ua.username_for("Asha Rao") == "asha.rao@merh.example"
    assert ua.username_for("  Meera Iyer ") == "meera.iyer@merh.example"


def test_generated_passwords_are_random_and_readable():
    pws = {ua.generate_password() for _ in range(200)}
    assert len(pws) == 200
    for p in list(pws)[:20]:
        assert len(p) == 14 and p.count("-") == 2
        assert not set(p.replace("-", "")) & set("0O1lI")          # no look-alike characters


def test_init_makes_a_distinct_account_for_every_employee():
    creds, shown = ua.build_credentials(all_records(), hasher=FAST)
    assert len(creds) == 6 and len(shown) == 6
    assert len({pw for _, pw in shown.values()}) == 6               # six different passwords
    for uid, (user, pw) in shown.items():
        assert creds[user]["user_id"] == uid
        assert check_password_hash(creds[user]["password_hash"], pw)


def test_only_hashes_are_written_to_disk():
    creds, shown = ua.build_credentials(all_records(), hasher=FAST)
    path = _tmp()
    ua.save_credentials(creds, path)
    text = path.read_text(encoding="utf-8")
    for _, pw in shown.values():
        assert pw not in text and pw.replace("-", "") not in text
    assert ua.load_credentials(path) == creds


def test_a_missing_file_gives_a_helpful_error():
    with pytest.raises(FileNotFoundError) as e:
        ua.load_credentials(_tmp())
    assert "users_admin init" in str(e.value)


def test_a_malformed_file_is_rejected():
    path = _tmp()
    path.write_text(json.dumps({"a@b.example": {"user_id": "E001"}}), encoding="utf-8")
    with pytest.raises(ValueError):
        ua.load_credentials(path)


def test_set_password_updates_only_that_account():
    creds, shown = ua.build_credentials(all_records(), hasher=FAST)
    before = {u: r["password_hash"] for u, r in creds.items()}
    user = ua.set_password(creds, "E001", "a-much-better-password", hasher=FAST)
    assert user == "asha.rao@merh.example"
    assert check_password_hash(creds[user]["password_hash"], "a-much-better-password")
    assert all(creds[u]["password_hash"] == h for u, h in before.items() if u != user)


@pytest.mark.parametrize("weak", ["short", "123456789", "", "asha.rao"])
def test_weak_passwords_are_refused(weak):
    creds, _ = ua.build_credentials(all_records(), hasher=FAST)
    with pytest.raises(ValueError):
        ua.set_password(creds, "E001", weak, hasher=FAST)


def test_set_password_for_an_unknown_user_is_refused():
    creds, _ = ua.build_credentials(all_records(), hasher=FAST)
    with pytest.raises(ValueError):
        ua.set_password(creds, "E999", "a-long-enough-password", hasher=FAST)


def test_init_refuses_to_overwrite_without_force():
    os.environ["CREDENTIALS_FILE"] = str(_tmp())
    try:
        assert ua.main(["init"]) == 0
        first = Path(os.environ["CREDENTIALS_FILE"]).read_text(encoding="utf-8")
        assert ua.main(["init"]) == 1                                  # refuses
        assert Path(os.environ["CREDENTIALS_FILE"]).read_text(encoding="utf-8") == first
        assert ua.main(["init", "--force"]) == 0                       # explicit replace
        assert Path(os.environ["CREDENTIALS_FILE"]).read_text(encoding="utf-8") != first
    finally:
        os.environ.pop("CREDENTIALS_FILE", None)
