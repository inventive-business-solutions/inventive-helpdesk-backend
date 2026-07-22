# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt

import time

import frappe
from frappe.tests import IntegrationTestCase

from inventive_helpdesk_backend import email as helpdesk_email
from inventive_helpdesk_backend.api import mark_ticket_read, unread_tickets


class TestReplyThreading(IntegrationTestCase):
    """Guards the two halves of email reply threading.

    Before this, a client replying to an acknowledgement opened a DUPLICATE ticket: the
    outgoing mail carried no reference, so Frappe had nothing to match the reply's
    In-Reply-To header against (frappe/email/receive.py:797). Observed live on
    2026-07-22 — a reply to INB-0002 created INB-0003 while INB-0002 kept an empty
    conversation.
    """

    def setUp(self):
        self.client = _ensure("Client", {"client_name": "Threading Co", "client_code": "THR"})
        self.division = _ensure(
            "Division", {"division_name": "Threading Div", "division_code": "TDV", "client": self.client}
        )

    def _ticket(self):
        return frappe.get_doc({
            "doctype": "Support Ticket",
            "title": "Threading fixture",
            "description": "original problem",
            "ticket_type": "Query",
            "priority": "Low",
            "status": "New",
            "client": self.client,
            "division": self.division,
            "from_email": "someone@example.test",
        }).insert(ignore_permissions=True)

    # ---- outgoing: the reference that makes threading possible ----
    def test_outgoing_mail_carries_the_ticket_reference(self):
        # The whole fix in one assertion. Without reference_doctype/reference_name on the
        # way out, the reply has nothing to match and forks a new ticket.
        captured = {}

        def fake_sendmail(**kwargs):
            captured.update(kwargs)

        original = frappe.sendmail
        frappe.sendmail = fake_sendmail
        try:
            helpdesk_email._queue_mail("client@example.test", "subj", "<p>body</p>", "test", "TKT-0001")
        finally:
            frappe.sendmail = original

        self.assertEqual(captured.get("reference_doctype"), "Support Ticket")
        self.assertEqual(captured.get("reference_name"), "TKT-0001")

    def test_mail_to_the_support_inbox_is_never_sent(self):
        # Loop protection: our own address must never receive our own mail.
        sent = []
        original = frappe.sendmail
        frappe.sendmail = lambda **kw: sent.append(kw)
        try:
            helpdesk_email._queue_mail(helpdesk_email._support_inbox(), "s", "<p>b</p>", "test", "TKT-0001")
        finally:
            frappe.sendmail = original
        self.assertEqual(sent, [])

    # ---- inbound: the reply has to become visible on the ticket ----
    def test_a_referenced_reply_appends_instead_of_opening_a_ticket(self):
        t = self._ticket()
        before = frappe.db.count("Support Ticket")

        comm = frappe.get_doc({
            "doctype": "Communication",
            "communication_type": "Communication",
            "communication_medium": "Email",
            "sent_or_received": "Received",
            "subject": f"Re: [{t.name}] Threading fixture",
            "sender": "someone@example.test",
            "content": "Here is the extra detail you asked for.",
            "reference_doctype": "Support Ticket",
            "reference_name": t.name,
        }).insert(ignore_permissions=True)
        helpdesk_email.on_communication(comm)

        self.assertEqual(frappe.db.count("Support Ticket"), before, "a reply must not create a ticket")
        t.reload()
        self.assertEqual([m.kind for m in t.conversation], ["client"])
        self.assertIn("extra detail", t.conversation[-1].body)
        self.assertTrue(t.last_activity_on, "a client reply must mark the ticket as having new activity")

    def test_the_same_reply_twice_appends_once(self):
        t = self._ticket()
        comm = frappe.get_doc({
            "doctype": "Communication",
            "communication_type": "Communication",
            "communication_medium": "Email",
            "sent_or_received": "Received",
            "subject": "Re: fixture",
            "sender": "someone@example.test",
            "content": "duplicate delivery",
            "reference_doctype": "Support Ticket",
            "reference_name": t.name,
        }).insert(ignore_permissions=True)
        helpdesk_email.on_communication(comm)
        helpdesk_email.on_communication(comm)

        t.reload()
        self.assertEqual(len(t.conversation), 1)

    def test_outgoing_communications_are_ignored(self):
        t = self._ticket()
        comm = frappe.get_doc({
            "doctype": "Communication",
            "communication_type": "Communication",
            "communication_medium": "Email",
            "sent_or_received": "Sent",
            "subject": "our own reply",
            "sender": "helpdesk@example.test",
            "content": "staff reply going out",
            "reference_doctype": "Support Ticket",
            "reference_name": t.name,
        }).insert(ignore_permissions=True)
        helpdesk_email.on_communication(comm)

        t.reload()
        self.assertEqual(len(t.conversation), 0, "our own outgoing mail must not echo into the thread")


class TestBodyCleaning(IntegrationTestCase):
    """Guards _clean_body: what a customer actually wrote, without the quoted thread or
    their signature.

    The bias is deliberately toward keeping text. Losing a customer's question is far
    worse than leaving a stray "Regards," on the end of it, so every rule here falls back
    to the original when it isn't confident.
    """

    def test_strips_a_gmail_quoted_reply(self):
        # The real shape observed from Gmail: the attribution wraps, so "wrote:" lands on
        # its own line and a single-line regex would miss it.
        raw = (
            "I don't know\r\n\r\nOn Wed, 22 Jul 2026, 18:21 Inventive Helpdesk, "
            "<helpdesk@inventivebizsol.com>\r\nwrote:\r\n\r\n> New reply on INB-0002\r\n> more"
        )
        self.assertEqual(helpdesk_email._clean_body(raw), "I don't know")

    def test_strips_outlook_quote_forms(self):
        for raw in (
            "The export still fails.\n\n-----Original Message-----\nFrom: Helpdesk\nold",
            "The export still fails.\n\nFrom: Helpdesk\nSent: Wednesday\nTo: me\n\nold",
            "The export still fails.\n\n____________________\nold",
        ):
            with self.subTest(raw=raw[:40]):
                self.assertEqual(helpdesk_email._clean_body(raw), "The export still fails.")

    def test_strips_signatures(self):
        for raw, expected in (
            ("Here is the log file.\n\n-- \nJane Doe\nAcme Ltd", "Here is the log file."),
            ("On my way.\n\nSent from my iPhone", "On my way."),
            ("Could you reopen ticket 42?\n\nRegards,\nRajesh Mehta\nThermax", "Could you reopen ticket 42?"),
        ):
            with self.subTest(raw=raw[:30]):
                self.assertEqual(helpdesk_email._clean_body(raw), expected)

    def test_decodes_html_entities(self):
        # strip_html only removes tags; entities survived it and rendered as &lt;.
        self.assertEqual(
            helpdesk_email._clean_body("<div>Contact &lt;ops@x.com&gt; &amp; confirm</div>"),
            "Contact <ops@x.com> & confirm",
        )

    def test_a_sign_off_word_mid_sentence_survives(self):
        # The false positive that would matter most: cutting at "Thanks" would throw away
        # the customer's actual problem.
        body = "Thanks for the update, but the issue is still happening on the second unit."
        self.assertEqual(helpdesk_email._clean_body(body), body)

    def test_a_body_that_is_only_a_signature_is_kept(self):
        # An early inbound ticket here was exactly this. Better a signature as the
        # description than an empty ticket.
        body = "Respectfully,\n\nAbhinav Bankar\nInventive Business Solutions"
        self.assertTrue(helpdesk_email._clean_body(body))

    def test_a_body_that_is_only_a_quote_is_kept(self):
        self.assertTrue(helpdesk_email._clean_body("> just the quoted text"))

    def test_multi_paragraph_bodies_survive_intact(self):
        body = "First paragraph.\n\nSecond with detail.\n\nThird one."
        self.assertEqual(helpdesk_email._clean_body(body), body)

    def test_empty_in_empty_out(self):
        self.assertEqual(helpdesk_email._clean_body(""), "")
        self.assertEqual(helpdesk_email._clean_body(None), "")

    def test_a_pasted_log_or_config_block_survives(self):
        # The worst bug this cleaner has had. `^\s*>` cut at the FIRST ">" line anywhere,
        # so a customer pasting a log lost the error AND the question after it. On a
        # technical support desk that is silent data loss on the highest-value messages.
        for raw in (
            "The log shows:\n> ERROR 500 at /api/export\nCan you check?",
            "Our config is:\n\n> timeout = 30\n> retries = 5\n\nIs that correct?",
        ):
            with self.subTest(raw=raw[:30]):
                self.assertEqual(helpdesk_email._clean_body(raw), raw)

    def test_a_trailing_quote_block_is_still_stripped(self):
        # The other half of the rule above: quoting IS quoting when it runs to the end.
        self.assertEqual(
            helpdesk_email._clean_body("Yes please.\n\n> old thread line 1\n> old thread line 2"),
            "Yes please.",
        )

    def test_prose_containing_wrote_is_not_read_as_a_quote_header(self):
        # "On <something> ... wrote:" without a date is ordinary prose. Requiring a date
        # in the attribution is what separates the two.
        body = (
            "Hi team,\n\nOn Monday the engineer said the valve was fine, but in his "
            "report he wrote: replace it. Which is right?"
        )
        self.assertEqual(helpdesk_email._clean_body(body), body)

    def test_first_contact_mail_keeps_its_forwarded_content(self):
        # A customer forwarding a supplier's rejection is a routine way a B2B ticket
        # arrives. There is no prior thread on a new ticket, so stripping "quotes" here
        # can only destroy the content that IS the ticket.
        raw = (
            "FYI please handle.\n\n---------- Forwarded message ---------\n"
            "From: Supplier <billing@acme.io>\nDate: Wed, 22 Jul 2026\nTo: <buyer@client.com>\n\n"
            "Your payment for INV-9912 was rejected by the bank. Reference 88213.\n"
        )
        self.assertIn("88213", helpdesk_email._clean_body(raw, is_reply=False))
        # As a REPLY the same text is correctly treated as quoted history.
        self.assertEqual(helpdesk_email._clean_body(raw), "FYI please handle.")

    def test_cleaning_a_large_body_stays_fast(self):
        # The Outlook header-block pattern used a lazy `.+?` under re.S anchored to `^From:`,
        # which restarted a scan to end-of-string at every "From:" line: 200 KB of mail
        # burned 6.5 SECONDS of CPU in a background worker, on attacker-supplied input
        # (anyone can email the public inbox). Now linear.
        big = "From: someone@example.com\n" * 16_000  # ~400 KB
        started = time.monotonic()
        helpdesk_email._clean_body(big)
        self.assertLess(time.monotonic() - started, 1.0, "quote matching went superlinear again")

    def test_localised_quote_markers(self):
        for raw, expected in (
            ("Bitte prüfen.\n\nAm Mi., 22. Juli 2026 um 18:21 Uhr schrieb H <h@x.com>:\n\n> alt", "Bitte prüfen."),
            ("Merci.\n\nLe mer. 22 juil. 2026 à 18:21, H <h@x.com> a écrit :\n\n> vieux", "Merci."),
            ("Revisar.\n\nEl mié, 22 jul 2026 a las 18:21, H <h@x.com> escribió:\n\n> viejo", "Revisar."),
            ("Bitte prüfen.\n\nVon: H\nGesendet: Mittwoch\nAn: mich\n\nalt", "Bitte prüfen."),
            ("Please check.\n\nH <h@x.com> wrote on 22/07/2026 18:21:\n\nold", "Please check."),
            ("Still failing.\n\n" + "-" * 40 + "\nold thread", "Still failing."),
        ):
            with self.subTest(raw=raw[:28]):
                self.assertEqual(helpdesk_email._clean_body(raw), expected)

    def test_html_only_mail_still_finds_the_quote_boundary(self):
        # Without rewriting block tags to newlines first, strip_html welds the body onto
        # the quote ("I don't knowOn Wed...") and every line-anchored marker fails.
        raw = (
            "<div dir=\"auto\">I don't know</div><br>"
            "<div class=\"gmail_quote\">On Wed, 22 Jul 2026 Helpdesk &lt;a@b.com&gt; wrote:<br>"
            "<blockquote>old thread</blockquote></div>"
        )
        self.assertEqual(helpdesk_email._clean_body(raw), "I don't know")


class TestUnreadMarkers(IntegrationTestCase):
    """The unread marker is per agent: one member reading a client reply must not clear
    it for the rest of the team."""

    def setUp(self):
        self.client = _ensure("Client", {"client_name": "Unread Co", "client_code": "UNR"})
        self.division = _ensure(
            "Division", {"division_name": "Unread Div", "division_code": "UDV", "client": self.client}
        )
        self.a = _staff("unread.agent.a@example.test")
        self.b = _staff("unread.agent.b@example.test")
        self.ticket = frappe.get_doc({
            "doctype": "Support Ticket",
            "title": "Unread fixture",
            "description": "x",
            "ticket_type": "Query",
            "priority": "Low",
            "status": "New",
            "client": self.client,
            "division": self.division,
        }).insert(ignore_permissions=True)
        self.ticket.db_set("last_activity_on", frappe.utils.now_datetime())

    def tearDown(self):
        frappe.set_user("Administrator")

    def test_activity_makes_a_ticket_unread_for_everyone_who_has_not_opened_it(self):
        frappe.set_user(self.a)
        self.assertIn(self.ticket.name, unread_tickets())
        frappe.set_user(self.b)
        self.assertIn(self.ticket.name, unread_tickets())

    def test_reading_clears_it_only_for_the_reader(self):
        # The reason this is a separate doctype rather than a flag on the ticket.
        frappe.set_user(self.a)
        mark_ticket_read(self.ticket.name)
        self.assertNotIn(self.ticket.name, unread_tickets())

        frappe.set_user(self.b)
        self.assertIn(self.ticket.name, unread_tickets(), "one agent reading must not clear it for another")

    def test_new_activity_after_a_read_marks_it_unread_again(self):
        frappe.set_user(self.a)
        mark_ticket_read(self.ticket.name)
        self.assertNotIn(self.ticket.name, unread_tickets())

        self.ticket.db_set("last_activity_on", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=1))
        self.assertIn(self.ticket.name, unread_tickets())

    def test_a_ticket_with_no_activity_is_never_unread(self):
        quiet = frappe.get_doc({
            "doctype": "Support Ticket",
            "title": "Quiet fixture",
            "description": "x",
            "ticket_type": "Query",
            "priority": "Low",
            "status": "New",
            "client": self.client,
            "division": self.division,
        }).insert(ignore_permissions=True)
        frappe.set_user(self.a)
        self.assertNotIn(quiet.name, unread_tickets())

    def test_a_client_poc_cannot_call_it(self):
        # Staff-only, same guard as every other staff method in api.py.
        poc_user = _website_user("unread.poc@example.test")
        frappe.set_user(poc_user)
        with self.assertRaises(frappe.PermissionError):
            unread_tickets()


def _ensure(doctype: str, values: dict) -> str:
    key = {k: v for k, v in values.items() if k in ("client_name", "division_name")}
    existing = frappe.db.get_value(doctype, key)
    return existing or frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True).name


def _staff(email: str) -> str:
    if not frappe.db.exists("User", email):
        u = frappe.get_doc({
            "doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0
        }).insert(ignore_permissions=True)
        u.add_roles("Support Team")
    return email


def _website_user(email: str) -> str:
    if not frappe.db.exists("User", email):
        frappe.get_doc({
            "doctype": "User", "email": email, "first_name": "poc",
            "user_type": "Website User", "send_welcome_email": 0,
        }).insert(ignore_permissions=True)
    return email
