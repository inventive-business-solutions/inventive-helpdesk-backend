# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt
"""Set-password links: how long they live, and who they stop working for.

Frappe mints one kind of key with one lifetime. We send it for two jobs that want opposite
windows — an invite is opened whenever the recipient next reads their mail, a reset is a
live account-takeover primitive sitting in an inbox — so the tighter window is enforced per
key here rather than in the one global setting.

The case that matters most is the last one. Frappe's update_password never checks
`enabled` and calls login_as() on success, so before this gate a revoked user holding an
unopened invite could set a password and be signed in to the account that had just been
closed. The check lives at REDEMPTION rather than at each of the several places that can
disable someone, so a disable path added later is covered without knowing this exists.
"""
import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime
from frappe.utils.data import sha256_hash

from inventive_helpdesk_backend.api import (
    INVITE_LINK_TTL_HOURS,
    LINK_EXPIRED,
    LINK_INVALID,
    LINK_REVOKED,
    LINK_VALID,
    RESET_LINK_TTL_HOURS,
    _resolve_password_key,
    password_link_status,
    set_password_with_key,
)

INVITEE = "_test.link.invitee@example.test"


def _user(email):
    if not frappe.db.exists("User", email):
        frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": "Link",
                "last_name": "Tester",
                "send_welcome_email": 0,
            }
        ).insert(ignore_permissions=True)
    return frappe.get_doc("User", email)


def _issue_key(user, *, hours_ago=0, ever_set_password=False):
    """Put a known key on the user and age it. Mirrors what _reset_password stores: the
    HASH in the column, the raw value in the link."""
    raw = frappe.generate_hash(length=32)
    user.db_set("reset_password_key", sha256_hash(raw))
    user.db_set("last_reset_password_key_generated_on", add_to_date(now_datetime(), hours=-hours_ago))
    # This is what tells the two link types apart: an account that has never had a password
    # set is being invited; one that has is resetting.
    user.db_set("last_password_reset_date", "2026-01-01" if ever_set_password else None)
    user.reload()
    return raw


def _clear_login_manager():
    try:
        del frappe.local.login_manager
    except Exception:
        pass


class _StubLoginManager:
    """update_password signs the user in on success, and `frappe.local.login_manager` only
    exists inside a real request. Stubbing it keeps these tests on OUR half of the contract
    — telling a refusal from a success, and stamping activation — instead of standing up
    Frappe's session machinery to observe a return value."""

    def __init__(self):
        self.user = None

    def login_as(self, user, *args, **kwargs):
        self.user = user


class TestPasswordLink(IntegrationTestCase):
    def setUp(self):
        self.user = _user(INVITEE)
        self.user.db_set("enabled", 1)
        self.user.reload()
        frappe.local.login_manager = _StubLoginManager()
        self.addCleanup(_clear_login_manager)

    def test_fresh_invite_is_valid(self):
        key = _issue_key(self.user)
        _, status = _resolve_password_key(key)
        self.assertEqual(status, LINK_VALID)

    def test_invite_survives_overnight(self):
        """The whole point of the wider invite window: mail read the next morning."""
        key = _issue_key(self.user, hours_ago=INVITE_LINK_TTL_HOURS - 1)
        _, status = _resolve_password_key(key)
        self.assertEqual(status, LINK_VALID)

    def test_invite_expires_after_its_window(self):
        key = _issue_key(self.user, hours_ago=INVITE_LINK_TTL_HOURS + 1)
        _, status = _resolve_password_key(key)
        self.assertEqual(status, LINK_EXPIRED)

    def test_reset_gets_the_tighter_window(self):
        """Same age, same key, different answer — because this account already has a
        password, which makes the link a reset rather than an invite."""
        age = RESET_LINK_TTL_HOURS + 1
        key = _issue_key(self.user, hours_ago=age, ever_set_password=True)
        _, status = _resolve_password_key(key)
        self.assertEqual(status, LINK_EXPIRED)

        # An invite of exactly the same age is still good.
        key = _issue_key(self.user, hours_ago=age, ever_set_password=False)
        _, status = _resolve_password_key(key)
        self.assertEqual(status, LINK_VALID)

    def test_unknown_and_empty_keys_are_invalid(self):
        for bad in ("", None, "not-a-real-key"):
            _, status = _resolve_password_key(bad)
            self.assertEqual(status, LINK_INVALID)

    def test_key_with_no_issue_time_fails_closed(self):
        key = _issue_key(self.user)
        self.user.db_set("last_reset_password_key_generated_on", None)
        _, status = _resolve_password_key(key)
        self.assertEqual(status, LINK_EXPIRED)

    def test_disabled_account_cannot_redeem_a_live_key(self):
        """The hole this was written for. The key is fresh and genuinely valid; the account
        has been closed. Frappe's update_password would have accepted it and signed them in."""
        key = _issue_key(self.user)
        self.user.db_set("enabled", 0)
        _, status = _resolve_password_key(key)
        self.assertEqual(status, LINK_REVOKED)

        with self.assertRaises(frappe.PermissionError):
            set_password_with_key(key=key, new_password="Str0ng-Passw0rd!x")

        # And the password really was not set — the account is still closed and keyless
        # from the app's point of view.
        self.assertFalse(frappe.db.get_value("User", INVITEE, "enabled"))

    def test_status_endpoint_leaks_nothing_but_the_status(self):
        key = _issue_key(self.user)
        out = password_link_status(key=key)
        self.assertEqual(out["status"], LINK_VALID)
        # No address, no name, nothing that says whose link this is or that the account
        # exists at any particular address.
        blob = frappe.as_json(out).lower()
        self.assertNotIn(INVITEE, blob)
        self.assertNotIn("link", blob.replace("link_", ""))  # no field named after the user

    def test_checking_the_status_does_not_consume_the_key(self):
        """Corporate mail scanners fetch every link before the human sees it. If arriving
        burned the key, scanned invites would be dead on opening."""
        key = _issue_key(self.user)
        for _ in range(3):
            self.assertEqual(password_link_status(key=key)["status"], LINK_VALID)
        _, status = _resolve_password_key(key)
        self.assertEqual(status, LINK_VALID)

    def test_expired_link_is_refused_at_redemption_too(self):
        """The page's pre-flight check is a courtesy; this is the boundary."""
        key = _issue_key(self.user, hours_ago=INVITE_LINK_TTL_HOURS + 1)
        with self.assertRaises(frappe.PermissionError):
            set_password_with_key(key=key, new_password="Str0ng-Passw0rd!x")

    def test_successful_redemption_returns_ok_and_spends_the_key(self):
        """The regression this file exists to prevent recurring.

        update_password returns a STRING on success too — a post-login redirect path — so
        the old `isinstance(result, str)` check threw frappe.PermissionError on every
        successful activation. Frappe rolls the request back on any exception, so the key
        update_password had just cleared came back, leaving the invite link redeemable
        again; the person meanwhile saw the raw path, or "Logged In", in the error slot.
        """
        key = _issue_key(self.user)
        out = set_password_with_key(key=key, new_password="Str0ng-Passw0rd!x")
        self.assertEqual(out, {"ok": True})
        # Spent, not merely reported as spent.
        self.assertFalse(frappe.db.get_value("User", INVITEE, "reset_password_key"))
        _, status = _resolve_password_key(key)
        self.assertEqual(status, LINK_INVALID)

    def test_redemption_activates_a_team_member(self):
        """The chip answers "has this person chosen a password?", so redeeming is what
        flips it — not signing in, which used to be inferred via an on_login hook."""
        member = frappe.get_doc(
            {
                "doctype": "Team Member",
                "member_name": "_Test Activation Member",
                "email": INVITEE,
                "status": "Invited",
                "user": INVITEE,
            }
        ).insert(ignore_permissions=True)
        self.addCleanup(frappe.delete_doc, "Team Member", member.name, force=True)

        set_password_with_key(key=_issue_key(self.user), new_password="Str0ng-Passw0rd!x")
        self.assertEqual(frappe.db.get_value("Team Member", member.name, "status"), "Active")

    def test_redemption_stamps_a_contact(self):
        client = frappe.get_doc(
            {"doctype": "Client", "client_name": "_Test Activation Client", "client_code": "TAC"}
        ).insert(ignore_permissions=True)
        self.addCleanup(frappe.delete_doc, "Client", client.name, force=True)
        poc = frappe.get_doc(
            {
                "doctype": "POC",
                "poc_name": "Activation Contact",
                "email": INVITEE,
                "client": client.name,
                "user": INVITEE,
                "invited_on": now_datetime(),
            }
        ).insert(ignore_permissions=True)
        self.addCleanup(frappe.delete_doc, "POC", poc.name, force=True)

        self.assertIsNone(frappe.db.get_value("POC", poc.name, "activated_on"))
        set_password_with_key(key=_issue_key(self.user), new_password="Str0ng-Passw0rd!x")
        self.assertIsNotNone(frappe.db.get_value("POC", poc.name, "activated_on"))
