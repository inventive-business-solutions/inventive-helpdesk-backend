# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from inventive_helpdesk_backend import email as helpdesk_email
from inventive_helpdesk_backend import sender


class TestSenderClassification(IntegrationTestCase):
    """Four sender kinds, because the obvious two-way split gets one of them wrong.

    A customer contact on file who was never invited has no portal login. Treating them as
    "registered" sends them "sign in to track your ticket" — a door with no key.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = _ensure("Client", {"client_name": "Sender Co", "client_code": "SND"})
        cls.division = _ensure(
            "Division", {"division_name": "Sender Div", "division_code": "SDV", "client": cls.client}
        )
        cls.registered = _poc(cls.client, cls.division, "registered.poc@example.test", with_login=True)
        cls.contact = _poc(cls.client, cls.division, "contact.only@example.test", with_login=False)

    def _ticket(self, from_email=None, **kw):
        return frappe.get_doc({
            "doctype": "Support Ticket",
            "title": "Sender fixture", "description": "x", "ticket_type": "Query",
            "priority": "Low", "status": "New",
            "client": self.client, "division": self.division,
            "from_email": from_email, **kw,
        }).insert(ignore_permissions=True)

    def test_the_four_kinds(self):
        for from_email, expected in (
            ("registered.poc@example.test", sender.REGISTERED),
            ("contact.only@example.test", sender.KNOWN_CONTACT),
            ("someone.we.dont.know@example.test", sender.UNREGISTERED),
            ("noreply@vendor.test", sender.NO_REPLY),
        ):
            with self.subTest(from_email=from_email):
                kind, addr, _reason = sender.classify(self._ticket(from_email))
                self.assertEqual(kind, expected)
                self.assertEqual(addr, from_email)

    def test_a_contact_with_no_login_is_not_registered(self):
        # The distinction the spec's two-state model loses. This POC is on file with the
        # right client and division — we just never invited them.
        kind, _addr, _r = sender.classify(self._ticket("contact.only@example.test"))
        self.assertEqual(kind, sender.KNOWN_CONTACT)
        self.assertIsNone(frappe.db.get_value("POC", {"email": "contact.only@example.test"}, "user"))

    def test_a_disabled_login_falls_back_to_known_contact(self):
        # The portal is equally unreachable whether the account was never made or was
        # switched off, so the reply channel is the same.
        email = "disabled.poc@example.test"
        _poc(self.client, self.division, email, with_login=True)
        frappe.db.set_value("User", email, "enabled", 0)
        try:
            kind, _addr, _r = sender.classify(self._ticket(email))
            self.assertEqual(kind, sender.KNOWN_CONTACT)
        finally:
            frappe.db.set_value("User", email, "enabled", 1)

    def test_a_ticket_with_no_contact_address_is_unregistered_and_unreachable(self):
        t = self._ticket(None, client=None, division=None)
        kind, addr, _r = sender.classify(t)
        self.assertEqual(kind, sender.UNREGISTERED)
        self.assertIsNone(addr)
        self.assertFalse(sender.can_receive_email(t))

    def test_the_cached_column_matches_the_derived_answer(self):
        t = self._ticket("contact.only@example.test")
        self.assertEqual(t.sender_kind, sender.KNOWN_CONTACT)

    def test_inviting_a_contact_upgrades_their_existing_tickets(self):
        """Edge case E8. Granting a login changes the answer without touching the ticket,
        so the cached column has to be refreshed or the badge stays wrong."""
        from inventive_helpdesk_backend.api import invite_poc

        email = "upgrade.me@example.test"
        poc = _poc(self.client, self.division, email, with_login=False)
        t = self._ticket(email)
        self.assertEqual(t.sender_kind, sender.KNOWN_CONTACT)

        invite_poc(poc)

        self.assertEqual(
            frappe.db.get_value("Support Ticket", t.name, "sender_kind"), sender.REGISTERED
        )


    def test_inviting_a_primary_contact_also_refreshes_agent_logged_tickets(self):
        """The half `from_email` cannot see.

        An agent-logged ticket carries no from_email at all — its reply address is
        resolved through the division's primary POC. Filtering the refresh on from_email
        missed every one of those, so inviting a division's main contact left their tickets
        badged "Known Contact" indefinitely.
        """
        from inventive_helpdesk_backend.api import invite_poc

        client = _ensure("Client", {"client_name": "Fallback Co", "client_code": "FBK"})
        division = _ensure(
            "Division", {"division_name": "Fallback Div", "division_code": "FBD", "client": client}
        )
        poc = _poc(client, division, "primary.contact@example.test", with_login=False)
        frappe.db.set_value("POC", poc, "is_primary", 1)

        t = frappe.get_doc({
            "doctype": "Support Ticket", "title": "Agent logged", "description": "x",
            "ticket_type": "Query", "priority": "Low", "status": "New",
            "client": client, "division": division,
        }).insert(ignore_permissions=True)
        self.assertIsNone(t.from_email, "vacuous test: this ticket must have no from_email")
        self.assertEqual(t.sender_kind, sender.KNOWN_CONTACT)

        invite_poc(poc)

        self.assertEqual(
            frappe.db.get_value("Support Ticket", t.name, "sender_kind"), sender.REGISTERED
        )


class TestReplyAddressHasOneImplementation(IntegrationTestCase):
    """Transport and classification must resolve the same address.

    email._ticket_contact_email was a second copy of sender.reply_address's fallback chain.
    They agreed, but nothing kept them agreeing — and if they drifted a ticket could be
    badged "Registered" while the mail went somewhere else entirely.
    """

    def test_email_delegates_rather_than_duplicating(self):
        client = _ensure("Client", {"client_name": "OneImpl Co", "client_code": "ONE"})
        division = _ensure(
            "Division", {"division_name": "OneImpl Div", "division_code": "OID", "client": client}
        )
        _poc(client, division, "oneimpl.primary@example.test", with_login=False)
        frappe.db.set_value("POC", "oneimpl.primary@example.test", "is_primary", 1)

        for kw in (
            {"from_email": "direct@example.test"},
            {"client": client, "division": division},  # resolved via the division fallback
        ):
            with self.subTest(kw=kw):
                t = frappe.get_doc({
                    "doctype": "Support Ticket", "title": "One impl", "description": "x",
                    "ticket_type": "Query", "priority": "Low", "status": "New", **kw,
                }).insert(ignore_permissions=True)
                self.assertEqual(helpdesk_email._ticket_contact_email(t), sender.reply_address(t))

class TestNoReplyDetection(IntegrationTestCase):
    """Detection is advisory: it never stops a ticket being created, so a false positive
    costs a warning badge rather than a customer's request."""

    def setUp(self):
        self._reset()

    def tearDown(self):
        self._reset()

    @staticmethod
    def _reset():
        frappe.db.delete("No Reply Rule", {"pattern": ["like", "_test.%"]})
        frappe.db.delete("No Reply Rule", {"pattern": "billing@partner.test"})
        sender.clear_rule_cache()
        # The ack budget is Redis state with a one-hour TTL, so it outlives the test run
        # and a repeated suite would exhaust it — making "an ack was sent" fail for a
        # reason that has nothing to do with the code under test.
        for who in ("a.real.person@example.test", "noreply@vendor.test"):
            frappe.cache().delete(helpdesk_email._ack_key(who))

    def test_addresses_that_announce_they_take_no_replies(self):
        # The built-ins cover only these — a mailbox whose own name says replies go
        # nowhere. Anything requiring a judgement call is an operator rule instead; see
        # test_plausible_shared_mailboxes_are_left_alone for why.
        for email in (
            "noreply@vendor.test", "no-reply@sap.test", "no_reply@sap.test",
            "donotreply@bank.test", "do-not-reply@bank.test",
            "bounce@list.test", "bounces@list.test", "automailer@erp.test",
        ):
            with self.subTest(email=email):
                self.assertIsNotNone(sender.no_reply_reason(email))

    def test_an_operator_rule_covers_what_the_built_ins_deliberately_miss(self):
        # The escape hatch that makes the narrow built-ins safe: a customer whose
        # `alerts@` really is unmonitored is one record away from being handled.
        self.assertIsNone(sender.no_reply_reason("alerts@monitor.test"))
        frappe.get_doc({
            "doctype": "No Reply Rule", "pattern": "alerts@monitor.test",
            "match_type": "Exact", "enabled": 1, "note": "confirmed unmonitored",
        }).insert(ignore_permissions=True)
        sender.clear_rule_cache()
        self.assertIsNotNone(sender.no_reply_reason("alerts@monitor.test"))
        frappe.db.delete("No Reply Rule", {"pattern": "alerts@monitor.test"})

    def test_a_real_person_is_not_matched(self):
        # Whole-local-part matching, not a substring. "noreply.patel@" is a person.
        for email in (
            "r.mehta@thermax.test", "noreply.patel@thermax.test",
            "alerts.manager@thermax.test", "system.admin@thermax.test",
        ):
            with self.subTest(email=email):
                self.assertIsNone(sender.no_reply_reason(email))

    def test_plausible_shared_mailboxes_are_left_alone(self):
        """The built-ins only match addresses that announce they take no replies.

        `alerts@` and `system@` at a customer's own domain are plausibly monitored, and
        guessing wrong is not cosmetic: a No Reply classification also withholds the
        acknowledgement, so the sender emails in and hears nothing — no ticket ID at all.
        Anything less than certain belongs in a No Reply Rule.
        """
        for email in (
            "alerts@thermax.test", "notifications@thermax.test",
            "system@thermax.test", "mailer@thermax.test", "automated@thermax.test",
        ):
            with self.subTest(email=email):
                self.assertIsNone(sender.no_reply_reason(email))

    def test_an_operator_rule_can_mark_an_address_unmonitored(self):
        # Layer 1 exists so a wrong built-in guess is fixed by a manager, not a release.
        frappe.get_doc({
            "doctype": "No Reply Rule", "pattern": "billing@partner.test",
            "match_type": "Exact", "enabled": 1,
        }).insert(ignore_permissions=True)
        sender.clear_rule_cache()
        self.assertIn("configured", sender.no_reply_reason("billing@partner.test") or "")

    def test_a_broken_regex_rule_cannot_break_intake(self):
        # The doctype validates regexes, but a rule could predate that or be written
        # straight to the database. Intake must survive it.
        rule = frappe.get_doc({
            "doctype": "No Reply Rule", "pattern": "_test.[unclosed",
            "match_type": "Exact", "enabled": 1,
        }).insert(ignore_permissions=True)
        # Straight to the column, bypassing validate() — the state this guards against.
        frappe.db.set_value("No Reply Rule", rule.name, "match_type", "Regex")
        sender.clear_rule_cache()
        self.assertIsNone(sender.no_reply_reason("r.mehta@thermax.test"))

    def test_a_no_reply_sender_still_gets_a_ticket_but_no_acknowledgement(self):
        """The spec is explicit that the ticket must still be created. It was not: the
        loop-guard work suppressed noreply@ senders entirely, so the mail vanished."""
        name = helpdesk_email._open_ticket_from_email(
            "noreply@vendor.test", "Invoice 8812 is ready", "Your invoice is attached."
        )
        self.assertIsNotNone(name, "a no-reply sender must still produce a ticket")
        self.assertEqual(frappe.db.get_value("Support Ticket", name, "sender_kind"), sender.NO_REPLY)

        self.assertEqual(_acks_sent_for(name), [], "acknowledging an unmonitored mailbox invites a bounce or a loop")

    def test_a_real_sender_still_gets_an_acknowledgement(self):
        # Guard against the check above being too broad.
        name = helpdesk_email._open_ticket_from_email(
            "a.real.person@example.test", "Help please", "The export fails."
        )
        self.assertEqual(len(_acks_sent_for(name)), 1)


def _ensure(doctype: str, values: dict) -> str:
    key = {k: v for k, v in values.items() if k in ("client_name", "division_name")}
    existing = frappe.db.get_value(doctype, key)
    return existing or frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True).name


def _poc(client: str, division: str, email: str, *, with_login: bool) -> str:
    if with_login and not frappe.db.exists("User", email):
        u = frappe.get_doc({
            "doctype": "User", "email": email, "first_name": "Test", "last_name": "POC",
            "user_type": "Website User", "send_welcome_email": 0,
        })
        u.append("roles", {"role": "Support Client"})
        u.insert(ignore_permissions=True)
    if not frappe.db.exists("POC", email):
        frappe.get_doc({
            "doctype": "POC", "poc_name": f"POC {email.split('@')[0]}", "email": email,
            "client": client, "division": division,
            "user": email if with_login else None,
        }).insert(ignore_permissions=True)
    return email


def _acks_sent_for(ticket_name: str) -> list:
    """Capture what send_ticket_ack would put on the wire for a ticket.

    `frappe.in_test` is in send_ticket_ack's skip flags, so calling it from a test sends
    nothing at all — which made an "it must not send" assertion pass for entirely the wrong
    reason. Lifting the flag is what makes both directions meaningful.
    """
    sent = []
    original_sendmail, original_in_test = frappe.sendmail, frappe.in_test
    frappe.sendmail = lambda **kw: sent.append(kw)
    frappe.in_test = False
    try:
        helpdesk_email.send_ticket_ack(frappe.get_doc("Support Ticket", ticket_name))
    finally:
        frappe.sendmail, frappe.in_test = original_sendmail, original_in_test
    return sent


class TestReplyPolicy(IntegrationTestCase):
    """Who gets a reply by email, decided server-side.

    The defect this closes: an unregistered sender's ticket could be answered with the
    email toggle off, so the agent typed a careful reply that reached nobody — there is no
    portal for that sender to read it in.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = _ensure("Client", {"client_name": "Policy Co", "client_code": "PCY"})
        cls.division = _ensure(
            "Division", {"division_name": "Policy Div", "division_code": "PDV", "client": cls.client}
        )
        _poc(cls.client, cls.division, "policy.registered@example.test", with_login=True)
        _poc(cls.client, cls.division, "policy.contact@example.test", with_login=False)

    def _ticket(self, from_email):
        return frappe.get_doc({
            "doctype": "Support Ticket", "title": "Policy fixture", "description": "x",
            "ticket_type": "Query", "priority": "Low", "status": "New",
            "client": self.client, "division": self.division, "from_email": from_email,
        }).insert(ignore_permissions=True)

    def test_a_sender_with_no_portal_is_always_emailed(self):
        # The toggle is not offered for these, and is ignored if a REST caller sends it.
        for addr in ("policy.contact@example.test", "nobody.knows@example.test"):
            for requested in (None, False, True):
                with self.subTest(addr=addr, requested=requested):
                    send, kind, _why = sender.reply_plan(self._ticket(addr), requested_email=requested)
                    self.assertTrue(send, "replying into a void — this sender has no portal")
                    self.assertEqual(kind, sender.FORCED)

    def test_a_registered_user_honours_the_toggle_once_they_have_been_told(self):
        t = self._ticket("policy.registered@example.test")
        t.db_set("first_response_notified_on", frappe.utils.now_datetime(), update_modified=False)
        t.reload()
        self.assertEqual(sender.reply_plan(t, requested_email=True)[0], True)
        self.assertEqual(sender.reply_plan(t, requested_email=False)[0], False)

    def test_the_first_reply_to_a_registered_user_goes_out_even_with_the_toggle_off(self):
        # Otherwise it sits unread in a portal they may never have opened.
        t = self._ticket("policy.registered@example.test")
        self.assertIsNone(t.first_response_notified_on)
        send, kind, _why = sender.reply_plan(t, requested_email=False)
        self.assertTrue(send)
        self.assertEqual(kind, sender.FIRST_RESPONSE)

    def test_a_no_reply_sender_is_never_emailed(self):
        for requested in (None, False, True):
            with self.subTest(requested=requested):
                send, kind, _why = sender.reply_plan(
                    self._ticket("noreply@vendor.test"), requested_email=requested
                )
                self.assertFalse(send)
                self.assertEqual(kind, sender.UNREACHABLE)

    def test_add_message_enforces_the_plan_and_stamps_the_first_response(self):
        """End to end through the whitelisted method, since the rule has to hold against a
        REST caller and not merely against the UI hiding a toggle."""
        from inventive_helpdesk_backend.api import add_message

        t = self._ticket("policy.registered@example.test")
        sent = []
        original = frappe.sendmail
        frappe.sendmail = lambda **kw: sent.append(kw)
        try:
            # Toggle explicitly OFF — the first reply must still go out.
            first = add_message(t.name, "We are looking into it.", send_email=0)
            self.assertTrue(first["emailed"])
            self.assertEqual(len(sent), 1)
            t.reload()
            self.assertIsNotNone(t.first_response_notified_on, "the one-time mail was not recorded")

            # Second reply, toggle still off — now it stays internal to the thread.
            second = add_message(t.name, "Still investigating.", send_email=0)
            self.assertFalse(second["emailed"])
            self.assertEqual(len(sent), 1, "a second mail went out after the client was pointed at the portal")
        finally:
            frappe.sendmail = original

    def test_every_outgoing_ticket_email_is_logged(self):
        from inventive_helpdesk_backend.api import add_message

        t = self._ticket("policy.contact@example.test")
        original = frappe.sendmail
        frappe.sendmail = lambda **kw: None
        try:
            add_message(t.name, "Emailed because they have no portal.")
        finally:
            frappe.sendmail = original

        rows = frappe.get_all(
            "Ticket Email Log", filters={"ticket": t.name}, fields=["kind", "recipient", "triggered_by"]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].recipient, "policy.contact@example.test")
        self.assertEqual(rows[0].kind, "Reply")
