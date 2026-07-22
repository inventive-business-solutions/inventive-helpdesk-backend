# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt

import json

import frappe
from frappe.tests import IntegrationTestCase

from inventive_helpdesk_backend.api import add_message, add_note

# Distinctive fixture names so tests never collide with real/seeded site data.
CLIENT_A = "_Test IC Client A"
CLIENT_B = "_Test IC Client B"
POC_A_EMAIL = "_test.ic.poc.a@example.com"
POC_B_EMAIL = "_test.ic.poc.b@example.com"


def make_client(name: str, code: str) -> str:
    if not frappe.db.exists("Client", name):
        frappe.get_doc({"doctype": "Client", "client_name": name, "client_code": code}).insert(
            ignore_permissions=True
        )
    return name


def make_division(client: str, dname: str, dcode: str) -> str:
    existing = frappe.db.get_value("Division", {"client": client, "division_code": dcode})
    if existing:
        return existing
    doc = frappe.get_doc({
        "doctype": "Division", "client": client, "division_name": dname, "division_code": dcode,
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def make_poc_user(email: str, client: str, division: str) -> str:
    if not frappe.db.exists("User", email):
        user = frappe.get_doc({
            "doctype": "User", "email": email, "first_name": "Test", "last_name": "POC",
            "user_type": "Website User", "send_welcome_email": 0,
        })
        user.append("roles", {"role": "Support Client"})
        user.insert(ignore_permissions=True)
    if not frappe.db.exists("POC", email):
        frappe.get_doc({
            "doctype": "POC", "poc_name": "Test POC", "email": email, "is_primary": 1,
            "client": client, "division": division, "user": email,
        }).insert(ignore_permissions=True)
    return email


def make_staff_outside_scope(email: str) -> str:
    """A Support Team agent with no assignment, no team and no collaboration — so every
    ticket in these fixtures is outside their read scope."""
    if not frappe.db.exists("User", email):
        user = frappe.get_doc({
            "doctype": "User", "email": email, "first_name": "Outside", "last_name": "Agent",
            "send_welcome_email": 0,
        })
        user.append("roles", {"role": "Support Team"})
        user.insert(ignore_permissions=True)
    return email


def make_ticket(**kw):
    doc = frappe.get_doc({
        "doctype": "Support Ticket",
        "title": kw.pop("title", "Test ticket"),
        "ticket_type": kw.pop("ticket_type", "Query"),
        "priority": kw.pop("priority", "Medium"),
        "status": kw.pop("status", "New"),
        **kw,
    })
    doc.insert(ignore_permissions=True)
    return doc


def make_member(name: str) -> str:
    if not frappe.db.exists("Team Member", name):
        frappe.get_doc({"doctype": "Team Member", "member_name": name}).insert(ignore_permissions=True)
    return name


def make_group(name: str) -> str:
    if not frappe.db.exists("Assignment Group", name):
        frappe.get_doc({"doctype": "Assignment Group", "group_name": name}).insert(ignore_permissions=True)
    return name


class TestSupportTicket(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client_a = make_client(CLIENT_A, "ZTA")
        cls.client_b = make_client(CLIENT_B, "ZTB")
        cls.div_a = make_division(cls.client_a, "Alpha", "ALP")
        cls.div_b = make_division(cls.client_b, "Beta", "BET")
        cls.poc_a = make_poc_user(POC_A_EMAIL, cls.client_a, cls.div_a)
        cls.poc_b = make_poc_user(POC_B_EMAIL, cls.client_b, cls.div_b)

    def tearDown(self):
        frappe.set_user("Administrator")
        super().tearDown()

    # ---- naming (atomic tabSeries counter) ----
    def test_autoname_sequences_per_division(self):
        t1 = make_ticket(client=self.client_a, division=self.div_a)
        t2 = make_ticket(client=self.client_a, division=self.div_a)
        self.assertTrue(t1.name.startswith("ZTA-ALP-"))
        n1 = int(t1.name.rsplit("-", 1)[1])
        n2 = int(t2.name.rsplit("-", 1)[1])
        self.assertEqual(n2, n1 + 1)

    def test_autoname_respects_legacy_floor(self):
        # A pre-series ticket (explicit name, like seeded data) must floor the counter.
        legacy = frappe.get_doc({
            "doctype": "Support Ticket", "name": "ZTB-BET-0042", "title": "Legacy",
            "ticket_type": "Query", "priority": "Medium", "status": "New",
            "client": self.client_b, "division": self.div_b,
        })
        legacy.flags.name_set = True
        legacy.insert(ignore_permissions=True)
        t = make_ticket(client=self.client_b, division=self.div_b)
        self.assertEqual(t.name, "ZTB-BET-0043")

    def test_unscoped_ticket_gets_inb_prefix(self):
        t = make_ticket(title="Unscoped")
        self.assertTrue(t.name.startswith("INB-"))

    def test_autoname_skips_past_explicit_insert_after_series_exists(self):
        # The floor only applies on a prefix's FIRST use. A later out-of-band
        # explicit-name insert (Data Import, manual backfill) can still land on
        # the exact number the series counter is about to issue — autoname must
        # skip past that collision instead of colliding or raising.
        client_c = make_client("_Test IC Client C", "ZTC")
        div_c = make_division(client_c, "Gamma", "GAM")
        t1 = make_ticket(client=client_c, division=div_c)  # series now at 1
        self.assertEqual(t1.name, "ZTC-GAM-0001")

        explicit = frappe.get_doc({
            "doctype": "Support Ticket", "name": "ZTC-GAM-0002", "title": "Explicit",
            "ticket_type": "Query", "priority": "Medium", "status": "New",
            "client": client_c, "division": div_c,
        })
        explicit.flags.name_set = True
        explicit.insert(ignore_permissions=True)

        t2 = make_ticket(client=client_c, division=div_c)
        self.assertEqual(t2.name, "ZTC-GAM-0003")

    # ---- server-side validation ----
    def test_division_must_belong_to_client(self):
        with self.assertRaises(frappe.ValidationError):
            make_ticket(client=self.client_b, division=self.div_a)

    def test_division_requires_client(self):
        with self.assertRaises(frappe.ValidationError):
            make_ticket(division=self.div_a)

    # ---- inbound email intake ----
    def test_email_intake_scopes_to_poc(self):
        t = make_ticket(title="From email", from_email=POC_A_EMAIL, ticket_type=None, priority=None)
        self.assertEqual(t.client, self.client_a)
        self.assertEqual(t.division, self.div_a)
        self.assertEqual(t.ticket_type, "Query")

    # ---- tenant isolation (permissions.py) ----
    def test_client_list_is_scoped_to_own_division(self):
        ta = make_ticket(client=self.client_a, division=self.div_a)
        tb = make_ticket(client=self.client_b, division=self.div_b)
        frappe.set_user(self.poc_a)
        visible = frappe.get_list("Support Ticket", pluck="name")
        self.assertIn(ta.name, visible)
        self.assertNotIn(tb.name, visible)
        clients = frappe.get_list("Client", pluck="name")
        self.assertEqual(clients, [self.client_a])

    def test_client_cannot_read_foreign_ticket(self):
        tb = make_ticket(client=self.client_b, division=self.div_b)
        frappe.set_user(self.poc_a)
        self.assertFalse(frappe.get_doc("Support Ticket", tb.name).has_permission("read"))

    # ---- whitelisted-method guards (api.py) ----
    def test_client_cannot_add_internal_note(self):
        ta = make_ticket(client=self.client_a, division=self.div_a)
        frappe.set_user(self.poc_a)
        with self.assertRaises(frappe.PermissionError):
            add_note(ta.name, "internal")

    def test_a_client_cannot_escape_the_clamp_by_sending_from_email(self):
        # `from_email` marks a ticket as email intake, and the clamp used to return early
        # on it. But it is permlevel 0 and Support Client holds `create`, so a POC could
        # set it in a REST payload and skip the clamp entirely: open the ticket
        # pre-Resolved, route it into a real team's queue, and append a conversation row
        # as kind="team" to forge a staff reply in their own thread.
        # Real Link targets, so the test turns on the clamp rather than on link validation.
        victim_member = make_member("_Test IC Victim Member")
        victim_group = make_group("_Test IC Victim Group")
        frappe.set_user(self.poc_a)
        t = frappe.get_doc({
            "doctype": "Support Ticket",
            "title": "Forged", "description": "x", "ticket_type": "Query", "priority": "Low",
            "client": self.client_a, "division": self.div_a,
            "from_email": "attacker@example.test",  # the bypass
            "status": "Resolved",
            "assignee": victim_member,
            "assignment_group": victim_group,
            "conversation": [{"kind": "team", "role": "Team → Client", "body": "we fixed it"}],
        }).insert()
        frappe.set_user("Administrator")
        t.reload()
        self.assertEqual(t.status, "New", "a client set the opening status")
        self.assertFalse(t.assignee, "a client self-assigned the ticket")
        self.assertFalse(t.assignment_group, "a client routed the ticket into a team queue")
        self.assertIsNone(t.from_email, "a client forged the intake marker")
        self.assertEqual([r.kind for r in t.conversation], ["client"], "a client forged a staff reply")

    def test_an_agent_cannot_note_on_a_ticket_they_cannot_read(self):
        # add_note guarded with _require_team only, which proves the caller is staff but
        # not that this ticket is in their scope. Agent tiers are scoped, so a bare
        # get_doc let any agent write an internal note onto any ticket by name.
        # Routed to a team the outsider is not in. An UNROUTED ticket would be readable by
        # design — assignment_group IS NULL is the shared triage inbox any agent may pick
        # up (permissions.ticket_query) — so it has to be routed for this to test anything.
        other_team = make_group("_Test IC Other Team")
        ta = make_ticket(client=self.client_a, division=self.div_a, assignment_group=other_team)
        outsider = make_staff_outside_scope("_test.ic.outsider@example.com")
        frappe.set_user(outsider)
        with self.assertRaises(frappe.PermissionError):
            add_note(ta.name, "should not land")
        frappe.set_user("Administrator")
        self.assertEqual(len(frappe.get_doc("Support Ticket", ta.name).notes), 0)

    def test_work_notes_stripped_from_client_read(self):
        # A client may read their OWN ticket, but internal work notes (permlevel-1
        # `notes` table) must never reach them. Reproduces the exact frontend read
        # path: /api/resource -> frappe.client.get -> check_permission (tenant scope)
        # + apply_fieldlevel_read_permissions (permlevel stripping) + as_dict.
        from frappe.client import get as client_get

        ta = make_ticket(client=self.client_a, division=self.div_a)
        add_note(ta.name, "internal-only diagnostics")  # staff attaches a note
        # Guard against a vacuous test: the note really is persisted on the ticket.
        self.assertEqual(len(frappe.get_doc("Support Ticket", ta.name).notes), 1)

        frappe.set_user(self.poc_a)
        served = client_get("Support Ticket", ta.name)
        self.assertEqual(served.get("name"), ta.name)  # tenant scope allows the read
        self.assertFalse(served.get("notes"), "internal work notes leaked to a client read")

    def test_client_can_message_own_ticket_only(self):
        ta = make_ticket(client=self.client_a, division=self.div_a)
        tb = make_ticket(client=self.client_b, division=self.div_b)
        frappe.set_user(self.poc_a)
        add_message(ta.name, "hello from the client")
        conv = frappe.get_doc("Support Ticket", ta.name).conversation
        self.assertEqual(conv[-1].kind, "client")
        self.assertEqual(conv[-1].body, "hello from the client")
        with self.assertRaises(frappe.PermissionError):
            add_message(tb.name, "should be blocked")

    def test_empty_message_rejected(self):
        ta = make_ticket(client=self.client_a, division=self.div_a)
        with self.assertRaises(frappe.ValidationError):
            add_message(ta.name, "   ")

    # ---- hardening: client-authored ticket create is clamped (M1) ----
    def test_client_created_ticket_fields_are_clamped(self):
        # A POC posting straight to the resource API could otherwise open a ticket
        # pre-Resolved, self-assign it, or inject a staff-labelled conversation row.
        # before_insert must clamp all of that for a client author.
        member = make_member("_Test IC Member")
        group = make_group("_Test IC Group")
        frappe.set_user(self.poc_a)
        doc = frappe.get_doc({
            "doctype": "Support Ticket",
            "title": "Client crafted",
            "ticket_type": "Bug",
            "priority": "High",
            "status": "Resolved",       # must be forced back to New
            "client": self.client_a,
            "division": self.div_a,
            "assignee": member,         # must be cleared
            "assignment_group": group,  # must be cleared
            "conversation": [{
                "kind": "team", "author": "Impersonated Staff", "role": "Team → Client",
                "message_on": "2026-07-16 10:00:00", "body": "forged staff reply",
            }],
        })
        doc.insert()
        self.assertEqual(doc.status, "New")
        self.assertIsNone(doc.assignee)
        self.assertIsNone(doc.assignment_group)
        self.assertEqual(doc.conversation[0].kind, "client")
        self.assertEqual(doc.conversation[0].role, "Client")

    def test_staff_created_ticket_status_is_preserved(self):
        # The clamp must NOT touch staff-authored tickets — Administrator (staff) here
        # legitimately opens a ticket in a non-default status.
        doc = make_ticket(client=self.client_a, division=self.div_a, status="In Progress")
        self.assertEqual(doc.status, "In Progress")

    # ---- hardening: provisioning refuses client/staff crossover (M2) ----
    def test_invite_refuses_client_to_staff_crossover(self):
        from inventive_helpdesk_backend.api import _ensure_login_user

        # POC_A_EMAIL already holds Support Client — provisioning it as staff must refuse,
        # else one login would sit on both sides of tenant isolation.
        with self.assertRaises(frappe.ValidationError):
            _ensure_login_user(POC_A_EMAIL, "Test POC", "System User", "Support Team")

    # ---- hardening: guest email webhook is inert outside developer mode (H1) ----
    def test_receive_webhook_blocked_outside_developer_mode(self):
        from inventive_helpdesk_backend.email import receive_webhook

        original = frappe.conf.get("developer_mode")
        try:
            frappe.conf.developer_mode = 0
            with self.assertRaises(frappe.PermissionError):
                receive_webhook()
        finally:
            frappe.conf.developer_mode = original

    # ---- hardening: ticket attachments are private + tenant-scoped (upload_attachment) ----
    def test_attachment_is_private_and_ticket_scoped(self):
        from inventive_helpdesk_backend.api import _attach_private_file

        ta = make_ticket(client=self.client_a, division=self.div_a)
        ref = _attach_private_file(
            frappe.get_doc("Support Ticket", ta.name), "log.txt", b"secret bytes", on_ticket=True
        )
        self.assertTrue(ref["url"])

        file_doc = frappe.get_doc("File", {"file_url": ref["url"]})
        self.assertEqual(file_doc.is_private, 1)
        self.assertEqual(file_doc.attached_to_doctype, "Support Ticket")
        self.assertEqual(file_doc.attached_to_name, ta.name)
        content = file_doc.get_content()
        self.assertEqual(content.encode() if isinstance(content, str) else content, b"secret bytes")

        # on_ticket recorded the reference on the ticket's description-level list.
        recorded = json.loads(frappe.db.get_value("Support Ticket", ta.name, "attachments") or "[]")
        self.assertEqual(recorded, [ref])

        # A private file's download derives from its attached ticket's permission, so tenant
        # isolation applies: the owning client can reach the ticket, a foreign client cannot.
        frappe.set_user(self.poc_a)
        self.assertTrue(frappe.has_permission("Support Ticket", ptype="read", doc=ta.name))
        frappe.set_user(self.poc_b)
        self.assertFalse(frappe.has_permission("Support Ticket", ptype="read", doc=ta.name))
