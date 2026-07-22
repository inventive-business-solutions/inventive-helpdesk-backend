# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt

import json
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

    def test_outgoing_mail_leaves_a_durable_anchor(self):
        # The Email Queue row frappe threads on is deleted after 30 days
        # (frappe/hooks.py:508), so it cannot be the only link back to the ticket. This
        # Communication is never purged and parent_communication() matches on its
        # message_id, so it keeps working after the queue row is gone.
        t = self._ticket()
        captured = {}
        original = frappe.sendmail
        frappe.sendmail = lambda **kw: captured.update(kw)
        try:
            helpdesk_email._queue_mail("client@example.test", "subj", "<p>body</p>", "test", t.name)
        finally:
            frappe.sendmail = original

        msg_id = captured.get("message_id")
        self.assertTrue(msg_id, "outgoing mail must carry a message id we control")
        self.assertNotIn("<", msg_id, "must be stored bare — the inbound side strips brackets")

        anchor = frappe.get_all(
            "Communication",
            filters={"message_id": msg_id, "sent_or_received": "Sent"},
            fields=["reference_doctype", "reference_name"],
        )
        self.assertEqual(len(anchor), 1, "no durable anchor was written")
        self.assertEqual(anchor[0].reference_doctype, "Support Ticket")
        self.assertEqual(anchor[0].reference_name, t.name)

    def test_a_reply_still_threads_after_the_email_queue_is_purged(self):
        # The actual 30-day scenario, driven through frappe's own resolution chain rather
        # than asserting on our own row: queue a mail, delete EVERY Email Queue row the way
        # the daily cleanup would, then ask frappe what the reply belongs to.
        from frappe.core.doctype.communication.communication import Communication

        t = self._ticket()
        captured = {}
        original = frappe.sendmail
        frappe.sendmail = lambda **kw: captured.update(kw)
        try:
            helpdesk_email._queue_mail("client@example.test", "subj", "<p>body</p>", "test", t.name)
        finally:
            frappe.sendmail = original
        msg_id = captured["message_id"]

        frappe.db.delete("Email Queue")  # what run_log_clean_up does on day 31

        # This is the lookup InboundMail.parent_communication() performs (receive.py:822)
        # with the reply's In-Reply-To header.
        parent = Communication.find_one_by_filters(message_id=msg_id, order_by="creation DESC")
        self.assertTrue(parent, "reply has nothing to thread onto once the queue row is gone")
        self.assertEqual(parent.reference_doctype, "Support Ticket")
        self.assertEqual(parent.reference_name, t.name)

    def test_the_anchor_does_not_echo_into_the_conversation(self):
        # It is a Sent Communication and our own after_insert hook fires on it. If that
        # hook ever stops ignoring Sent mail, every outbound message would appear in the
        # client thread twice.
        t = self._ticket()
        original = frappe.sendmail
        frappe.sendmail = lambda **kw: None
        try:
            helpdesk_email._queue_mail("client@example.test", "subj", "<p>body</p>", "test", t.name)
        finally:
            frappe.sendmail = original
        t.reload()
        self.assertEqual(len(t.conversation), 0)

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


class TestInboundAttachments(IntegrationTestCase):
    """A file a customer emails in has to end up ON the ticket.

    Frappe attaches inbound files to the Communication (receive.py:632-645), and it does
    so AFTER inserting it — so the after_insert hook that handles the body sees no files
    at all. "Here's a screenshot of the error" is routine for after-sales support, and it
    used to leave the agent reading a body referring to a file the ticket did not have.
    """

    def setUp(self):
        self.client = _ensure("Client", {"client_name": "Attach Co", "client_code": "ATT"})
        self.division = _ensure(
            "Division", {"division_name": "Attach Div", "division_code": "ADV", "client": self.client}
        )
        self.ticket = frappe.get_doc({
            "doctype": "Support Ticket",
            "title": "Attachment fixture", "description": "x", "ticket_type": "Query",
            "priority": "Low", "status": "New",
            "client": self.client, "division": self.division,
        }).insert(ignore_permissions=True)

    def _received(self):
        return frappe.get_doc({
            "doctype": "Communication",
            "communication_type": "Communication", "communication_medium": "Email",
            "sent_or_received": "Received", "subject": "with a file",
            "sender": "someone@example.test", "content": "see attached",
            "reference_doctype": "Support Ticket", "reference_name": self.ticket.name,
        }).insert(ignore_permissions=True)

    def _attach_to(self, comm, name="screenshot.png", content=b"not-really-a-png"):
        return frappe.get_doc({
            "doctype": "File", "file_name": name,
            "attached_to_doctype": "Communication", "attached_to_name": comm.name,
            "is_private": 1, "content": content,
        }).insert(ignore_permissions=True)

    def test_an_emailed_file_lands_on_the_ticket(self):
        comm = self._received()
        self._attach_to(comm)
        helpdesk_email.on_communication_update(comm)

        moved = frappe.get_all(
            "File",
            filters={"attached_to_doctype": "Support Ticket", "attached_to_name": self.ticket.name},
            fields=["file_name", "is_private"],
        )
        self.assertEqual([f.file_name for f in moved], ["screenshot.png"])
        self.assertEqual(moved[0].is_private, 1, "an emailed file must not become world-readable")

        listed = json.loads(frappe.db.get_value("Support Ticket", self.ticket.name, "attachments") or "[]")
        self.assertEqual([r["name"] for r in listed], ["screenshot.png"],
                         "the file moved but the ticket does not list it, so the portal won't show it")

    def test_the_owning_client_can_read_it_and_a_foreign_one_cannot(self):
        """The reason this re-parents rather than copies.

        File.has_permission delegates to whatever the file is attached to
        (frappe/core/doctype/file/file.py:967). On a Communication that means a client POC
        cannot read their OWN attachment — clients have no Communication access at all. On
        the ticket it inherits the tenant isolation already enforced there, so the right
        client gains access and the wrong one still has none."""
        comm = self._received()
        f = self._attach_to(comm)

        mine = _poc_for(self.client, self.division, "attach.poc@example.test")
        other_client = _ensure("Client", {"client_name": "Attach Rival", "client_code": "ATR"})
        other_div = _ensure(
            "Division",
            {"division_name": "Rival Div", "division_code": "RVD", "client": other_client},
        )
        theirs = _poc_for(other_client, other_div, "rival.poc@example.test")

        frappe.set_user(mine)
        self.assertFalse(
            frappe.get_doc("File", f.name).has_permission("read"),
            "vacuous test: the owning client could already read it before re-parenting",
        )
        frappe.set_user("Administrator")

        helpdesk_email.on_communication_update(comm)

        frappe.set_user(mine)
        self.assertTrue(
            frappe.get_doc("File", f.name).has_permission("read"),
            "the client this ticket belongs to still cannot download their own attachment",
        )
        frappe.set_user(theirs)
        self.assertFalse(
            frappe.get_doc("File", f.name).has_permission("read"),
            "another client can read an attachment on a ticket that is not theirs",
        )
        frappe.set_user("Administrator")

    def test_running_twice_does_not_duplicate(self):
        # on_update fires more than once per Communication.
        comm = self._received()
        self._attach_to(comm)
        helpdesk_email.on_communication_update(comm)
        helpdesk_email.on_communication_update(comm)
        listed = json.loads(frappe.db.get_value("Support Ticket", self.ticket.name, "attachments") or "[]")
        self.assertEqual(len(listed), 1)

    def test_our_own_outgoing_mail_is_left_alone(self):
        comm = self._received()
        comm.db_set("sent_or_received", "Sent", update_modified=False)
        comm.reload()
        self._attach_to(comm)
        helpdesk_email.on_communication_update(comm)
        self.assertFalse(
            json.loads(frappe.db.get_value("Support Ticket", self.ticket.name, "attachments") or "[]")
        )

    def test_a_communication_with_no_ticket_is_ignored(self):
        comm = self._received()
        comm.db_set("reference_doctype", None, update_modified=False)
        comm.db_set("reference_name", None, update_modified=False)
        comm.reload()
        self._attach_to(comm)
        helpdesk_email.on_communication_update(comm)  # must not raise
        still_there = frappe.get_all(
            "File", filters={"attached_to_doctype": "Communication", "attached_to_name": comm.name}
        )
        self.assertEqual(len(still_there), 1, "left where frappe put it")


class TestBounceHandling(IntegrationTestCase):
    """A delivery failure belongs on the ticket whose mail failed.

    Before this it did the opposite of useful: MAILER-DAEMON opened a junk ticket, while
    the ticket that actually failed to reach its customer looked healthy — so the agent
    believed they had replied when nobody had received anything.
    """

    def setUp(self):
        self.client = _ensure("Client", {"client_name": "Bounce Co", "client_code": "BNC"})
        self.division = _ensure(
            "Division", {"division_name": "Bounce Div", "division_code": "BDV", "client": self.client}
        )
        self.ticket = frappe.get_doc({
            "doctype": "Support Ticket",
            "title": "Bounce fixture", "description": "x", "ticket_type": "Query",
            "priority": "Low", "status": "New",
            "client": self.client, "division": self.division,
        }).insert(ignore_permissions=True)

    def _dsn(self, subject, body, sender="MAILER-DAEMON@mail.example.test"):
        return frappe.get_doc({
            "doctype": "Communication",
            "communication_type": "Communication", "communication_medium": "Email",
            "sent_or_received": "Received", "subject": subject,
            "sender": sender, "content": body,
        }).insert(ignore_permissions=True)

    def test_a_bounce_is_filed_on_the_ticket_that_sent_the_mail(self):
        # Exchange's shape: ticket id in the DSN subject.
        before = frappe.db.count("Support Ticket")
        comm = self._dsn(
            f"Undeliverable: [{self.ticket.name}] Bounce fixture",
            "Your message couldn't be delivered to r.mehta@thermax.test.\n"
            "550 5.1.1 The email address you entered couldn't be found.",
        )
        helpdesk_email.on_communication(comm)

        self.assertEqual(frappe.db.count("Support Ticket"), before, "a bounce opened a ticket")
        self.ticket.reload()
        self.assertEqual(len(self.ticket.notes), 1, "the bounce was dropped instead of filed")
        note = self.ticket.notes[0].body
        self.assertIn("Delivery failed", note)
        self.assertIn("r.mehta@thermax.test", note, "the failed address is what an agent needs")
        self.assertIn("550", note, "the reason from the DSN should survive")
        self.assertTrue(self.ticket.last_activity_on, "the team must see the ticket as needing attention")

    def test_the_ticket_id_is_found_in_the_body_when_the_subject_lacks_it(self):
        # Postfix's shape: generic subject, original subject quoted in the body.
        comm = self._dsn(
            "Undelivered Mail Returned to Sender",
            "This is the mail system at host mx.example.test.\n\n"
            "<r.mehta@thermax.test>: host mx.thermax.test said: 550 unknown user\n\n"
            f"Subject: [{self.ticket.name}] Bounce fixture",
        )
        helpdesk_email.on_communication(comm)
        self.ticket.reload()
        self.assertEqual(len(self.ticket.notes), 1)

    def test_a_bounce_it_cannot_place_still_does_not_open_a_ticket(self):
        # A bounce is never a support request, so "no ticket" beats "junk ticket" even
        # when we cannot tell which ticket it came from.
        before = frappe.db.count("Support Ticket")
        comm = self._dsn("Undeliverable: some other mail", "550 mailbox unavailable")
        helpdesk_email.on_communication(comm)
        self.assertEqual(frappe.db.count("Support Ticket"), before)

    def test_the_same_bounce_twice_is_filed_once(self):
        subject = f"Undeliverable: [{self.ticket.name}] Bounce fixture"
        body = "Your message couldn't be delivered to r.mehta@thermax.test. 550 not found."
        helpdesk_email.on_communication(self._dsn(subject, body))
        helpdesk_email.on_communication(self._dsn(subject, body))
        self.ticket.reload()
        self.assertEqual(len(self.ticket.notes), 1)

    def test_the_bounce_is_staff_only(self):
        # A customer must not read "we could not reach you" — it is a work note, and work
        # notes are permlevel 1. Drives the real client read path.
        from frappe.client import get as client_get

        poc = _poc_for(self.client, self.division, "bounce.poc@example.test")
        comm = self._dsn(f"Undeliverable: [{self.ticket.name}] x", "550 nope")
        helpdesk_email.on_communication(comm)
        self.ticket.reload()
        self.assertEqual(len(self.ticket.notes), 1)  # guard against a vacuous assertion

        frappe.set_user(poc)
        try:
            served = client_get("Support Ticket", self.ticket.name)
        finally:
            frappe.set_user("Administrator")
        self.assertFalse(served.get("notes"), "a delivery failure leaked to the client")

    def test_a_genuine_customer_mail_is_not_mistaken_for_a_bounce(self):
        """The false positive that costs a customer their ticket.

        This desk's customers talk about shipments and deliveries, so failure words in a
        subject are ordinary. A bounce needs a daemon sender, or DSN structure in the body
        to corroborate the subject — neither is present here."""
        for subject in (
            "Report is undeliverable to site B - please advise",
            "Delivery has failed for our shipment, can you check the ticket?",
            "Returned mail from the courier - who do we contact?",
            "Undeliverable stock at the Pune warehouse",
        ):
            with self.subTest(subject=subject):
                body = "Please advise, this is blocking the commissioning."
                self.assertFalse(helpdesk_email._is_bounce("r.mehta@thermax.test", subject, body))
                self.assertFalse(helpdesk_email._is_auto_generated("r.mehta@thermax.test", subject))

    def test_a_relayed_bounce_still_counts_when_the_body_proves_it(self):
        # Not every DSN comes from mailer-daemon. A failure subject PLUS real DSN structure
        # is enough; the subject alone is not.
        dsn = "Final-Recipient: rfc822; r.mehta@thermax.test\nAction: failed\nStatus: 5.1.1"
        self.assertTrue(helpdesk_email._is_bounce("relay@corp.example", "Undeliverable: x", dsn))
        self.assertFalse(helpdesk_email._is_bounce("relay@corp.example", "Undeliverable: x", "hello"))


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


class TestLoopProtection(IntegrationTestCase):
    """Guards the two independent brakes on an autoresponder loop.

    send_ticket_ack fires on every insert carrying a from_email, so a correspondent whose
    autoresponder strips threading headers gives inbound -> ticket -> ack -> autoreply ->
    ticket -> ack, without bound. On a live tenant that is a throttled domain.
    """

    def setUp(self):
        self._reset_budget()

    def tearDown(self):
        self._reset_budget()

    @staticmethod
    def _reset_budget():
        # Raw `delete`, matching the raw `incr` the limiter uses. frappe's delete_value
        # would prefix the site a second time and clear a key nothing ever wrote. The
        # budget also outlives a test run (1h TTL), so it has to be cleared BOTH sides.
        for who in ("loop.test@example.test", "someone.else@example.test"):
            frappe.cache().delete(helpdesk_email._ack_key(who))

    # ---- brake 1: don't open a ticket from machine-sent mail ----
    def test_bounces_and_out_of_office_do_not_open_tickets(self):
        for sender, subject in (
            ("MAILER-DAEMON@mail.example.test", "Undeliverable: [INB-0002] Test"),
            ("postmaster@example.test", "Delivery Status Notification (Failure)"),
            ("noreply@vendor.test", "Your receipt"),
            ("real.person@client.test", "Automatic reply: Out of the office"),
            ("real.person@client.test", "Re: Re: Out of office until 3 August"),
            ("real.person@client.test", "Abwesenheitsnotiz: Ihre Anfrage"),
        ):
            with self.subTest(sender=sender, subject=subject):
                self.assertTrue(helpdesk_email._is_auto_generated(sender, subject))

    def test_a_real_request_that_merely_mentions_the_phrase_still_opens_a_ticket(self):
        # The false positive that would cost a customer their ticket. These are anchored
        # at the start of the subject precisely so a question ABOUT out-of-office is safe.
        for sender, subject in (
            ("r.mehta@thermax.test", "How do I set an out of office in the portal?"),
            ("r.mehta@thermax.test", "Export undeliverable to site B - urgent"),
            ("r.mehta@thermax.test", "Re: [THX-HTG-0042] Valve symbols mis-detected"),
            ("Rajesh Mehta <r.mehta@thermax.test>", "Automatic tag validation is failing"),
        ):
            with self.subTest(subject=subject):
                self.assertFalse(helpdesk_email._is_auto_generated(sender, subject))

    def test_an_auto_reply_creates_no_ticket_through_the_real_intake(self):
        before = frappe.db.count("Support Ticket")
        name = helpdesk_email._open_ticket_from_email(
            "MAILER-DAEMON@mail.example.test", "Undeliverable: your message", "delivery failed"
        )
        self.assertIsNone(name)
        self.assertEqual(frappe.db.count("Support Ticket"), before)

    # ---- brake 2: the cap that bounds a loop whatever caused it ----
    def test_acks_to_one_recipient_are_capped_per_hour(self):
        who = "loop.test@example.test"
        allowed = [helpdesk_email._ack_allowed(who) for _ in range(helpdesk_email._ACK_CAP_PER_HOUR + 3)]
        self.assertEqual(allowed[: helpdesk_email._ACK_CAP_PER_HOUR],
                         [True] * helpdesk_email._ACK_CAP_PER_HOUR)
        self.assertEqual(allowed[helpdesk_email._ACK_CAP_PER_HOUR:], [False, False, False],
                         "the cap has to keep holding, not just trip once")

    def test_the_cap_is_per_recipient(self):
        # One looping correspondent must not silence acknowledgements for everyone else.
        for _ in range(helpdesk_email._ACK_CAP_PER_HOUR + 1):
            helpdesk_email._ack_allowed("loop.test@example.test")
        self.assertTrue(helpdesk_email._ack_allowed("someone.else@example.test"))

    # ---- outbound: don't provoke the auto-reply in the first place ----
    def test_outgoing_mail_asks_recipients_not_to_auto_reply(self):
        captured = {}
        original = frappe.sendmail
        frappe.sendmail = lambda **kw: captured.update(kw)
        try:
            helpdesk_email._queue_mail("client@example.test", "s", "<p>b</p>", "test", "TKT-0001")
        finally:
            frappe.sendmail = original
        self.assertEqual(captured.get("email_headers"), {"X-Auto-Response-Suppress": "All"})

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


def _poc_for(client: str, division: str, email: str) -> str:
    if not frappe.db.exists("User", email):
        u = frappe.get_doc({
            "doctype": "User", "email": email, "first_name": "poc",
            "user_type": "Website User", "send_welcome_email": 0,
        })
        u.append("roles", {"role": "Support Client"})
        u.insert(ignore_permissions=True)
    if not frappe.db.exists("POC", email):
        frappe.get_doc({
            "doctype": "POC", "poc_name": "Attach POC", "email": email, "is_primary": 1,
            "client": client, "division": division, "user": email,
        }).insert(ignore_permissions=True)
    return email
