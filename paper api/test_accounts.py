"""Account ids: the identity a member is known by everywhere outside this database.

Run from THIS directory, alongside the other two suites::

    ..\\.venv\\Scripts\\python -m pytest test_auth.py test_board.py test_accounts.py -q

The property being protected is that an `account_id`, once issued, is **permanent** and is
never handed to somebody else. Everything the trading engine records — strategies, fills,
curve points — is keyed on it, and the engine has no way to notice that the person behind
an id has changed.

It is deliberately not *unique per address*: one account may have several sign-in
addresses, which is the same person with two mailboxes wanting one book. Sharing an id is
therefore possible, but only by naming both addresses (`link_account`), never by accident.
"""

from __future__ import annotations

import pytest

import api_paths                                                        # noqa: F401
import authdb


@pytest.fixture()
def db(tmp_path):
    authdb.use(tmp_path / "auth.db")
    authdb.connect()
    yield authdb
    authdb.close()


def test_ids_are_issued_in_order_and_skip_the_house(db):
    """`00` is the desk's own book and must never be handed to a person."""
    assert db.allow("first@example.com")["account_id"] == "01"
    assert db.allow("second@example.com")["account_id"] == "02"
    assert db.HOUSE_ACCOUNT == "00"
    ids = {u["account_id"] for u in db.users()}
    assert db.HOUSE_ACCOUNT not in ids


def test_encoding_widens_rather_than_wrapping(db):
    """Wrapping would reissue a live id, which merges two people's books."""
    assert db._encode_account(1) == "01"
    assert db._encode_account(35) == "0z"
    assert db._encode_account(36) == "10"
    assert db._encode_account(1295) == "zz"
    assert db._encode_account(1296) == "100"
    for n in (1, 35, 36, 1295, 1296, 50_000):
        assert db._decode_account(db._encode_account(n)) == n


def test_reallowing_an_address_keeps_its_id(db):
    """The failure this prevents is silent: re-running `admin_users.py allow` on somebody
    already on the list would mint a new id and orphan every row they own on the desk."""
    original = db.allow("m@example.com", label="M")["account_id"]
    db.allow("m@example.com", label="M renamed", is_admin=True)
    assert db.user("m@example.com")["account_id"] == original


def test_revoking_and_reallowing_keeps_the_id(db):
    """A revoked manager who comes back must find their track record, not a blank one."""
    original = db.allow("m@example.com")["account_id"]
    db.revoke("m@example.com")
    db.allow("m@example.com")
    assert db.user("m@example.com")["account_id"] == original


def test_a_purged_account_id_is_never_reused(db):
    """Lowest-free would hand a purged member's id — and their strategies, fills and
    curve — to whoever registered next. The engine keys on the id and cannot tell."""
    gone = db.allow("gone@example.com")["account_id"]
    db.allow("stays@example.com")
    db.purge("gone@example.com")
    fresh = db.allow("new@example.com")["account_id"]
    assert fresh != gone
    assert db._decode_account(fresh) > db._decode_account(gone)


def test_allocation_never_hands_out_a_live_id(db):
    """Ids are no longer UNIQUE in the schema — one account may have several sign-in
    addresses — so what protects two people's books from merging is allocation, not a
    constraint. `next_account_id` is one past the highest ever issued, so it cannot
    collide with a live id, a revoked one, or a purged one.

    Merging is still possible, but only by naming both addresses: `link_account`.
    """
    issued = {db.allow(f"u{i}@example.com")["account_id"] for i in range(12)}
    assert len(issued) == 12, "allocation handed out a duplicate"

    db.revoke("u3@example.com")
    db.purge("u4@example.com")
    fresh = db.allow("later@example.com")["account_id"]
    assert fresh not in issued


def test_both_lookups_agree(db):
    db.allow("m@example.com")
    account = db.account_id("m@example.com")
    assert db.email_for_account(account) == "m@example.com"
    assert db.email_for_account("zz") is None
    assert db.account_id("nobody@example.com") is None


def test_an_existing_allowlist_is_backfilled_reproducibly(tmp_path):
    """An allowlist predating this column gets ids assigned in a stable order, so the
    same database migrated twice on two machines produces the same mapping."""
    import sqlite3

    def build(path):
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE users (
                email TEXT PRIMARY KEY, label TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, last_login_at TEXT);
            INSERT INTO users (email, created_at) VALUES
                ('zoe@example.com',  '2026-01-03T00:00:00+00:00'),
                ('adam@example.com', '2026-01-01T00:00:00+00:00'),
                ('mary@example.com', '2026-01-02T00:00:00+00:00');
        """)
        conn.commit()
        conn.close()

    mappings = []
    for name in ("one.db", "two.db"):
        path = tmp_path / name
        build(path)
        authdb.use(path)
        authdb.connect()
        mappings.append({u["email"]: u["account_id"] for u in authdb.users()})
        authdb.close()

    assert mappings[0] == mappings[1]
    # Ordered by created_at, so the earliest member is 01 whatever their address.
    assert mappings[0]["adam@example.com"] == "01"
    assert mappings[0]["mary@example.com"] == "02"
    assert mappings[0]["zoe@example.com"] == "03"


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "auth.db"
    authdb.use(path)
    first = db_ids = None
    authdb.connect()
    authdb.allow("m@example.com")
    first = authdb.account_id("m@example.com")
    authdb.close()

    authdb.use(path)                 # a second process opening the same file
    authdb.connect()
    db_ids = authdb.account_id("m@example.com")
    authdb.close()
    assert db_ids == first


# ------------------------------------------------------- one account, several addresses

def test_two_addresses_can_share_one_account(db):
    """The same person with two mailboxes wants ONE book, not two."""
    first = db.allow("a@example.com")["account_id"]
    db.allow("b@example.com")
    shared = db.link_account("b@example.com", "a@example.com")

    assert shared == first
    assert db.account_id("b@example.com") == first
    assert set(db.emails_for_account(first)) == {"a@example.com", "b@example.com"}


def test_linking_is_the_only_way_to_share(db):
    """Allocation still never collides on its own — merging has to be a named act,
    because it is the one operation that can join two people's records."""
    a = db.allow("a@example.com")["account_id"]
    b = db.allow("b@example.com")["account_id"]
    assert a != b


def test_a_linked_address_keeps_its_own_credentials(db):
    """Only the account is shared. Sessions, keys and revocation stay per address —
    otherwise revoking a lost laptop's mailbox would lock out the other one too."""
    db.allow("a@example.com")
    db.allow("b@example.com")
    shared = db.link_account("b@example.com", "a@example.com")

    _, key_b = db.create_api_key("b@example.com")
    db.revoke("b@example.com")

    assert db.api_key(db.hash_key("x")) is None
    assert db.api_keys_for("b@example.com", live_only=True) == []
    # The other address is untouched and still reaches the same account.
    assert db.active_user("a@example.com") is not None
    assert db.account_id("a@example.com") == shared


def test_email_for_account_is_stable_when_several_share_it(db):
    """It returns the earliest by creation, not whichever row came back first."""
    db.allow("first@example.com")
    db.allow("second@example.com")
    account = db.link_account("second@example.com", "first@example.com")
    assert db.email_for_account(account) == "first@example.com"
    assert db.email_for_account(account) == "first@example.com"


@pytest.mark.parametrize("a,b,why", [
    ("nobody@example.com", "a@example.com", "not on the allowlist"),
    ("a@example.com", "nobody@example.com", "not on the allowlist"),
    ("a@example.com", "a@example.com", "same address"),
])
def test_bad_links_are_refused(db, a, b, why):
    db.allow("a@example.com")
    with pytest.raises(ValueError, match=why):
        db.link_account(a, b)


def test_relinking_moves_the_address_again(db):
    db.allow("a@example.com")
    db.allow("b@example.com")
    db.allow("c@example.com")
    db.link_account("c@example.com", "a@example.com")
    moved = db.link_account("c@example.com", "b@example.com")
    assert db.account_id("c@example.com") == moved
    assert db.emails_for_account(moved) == ["b@example.com", "c@example.com"]
