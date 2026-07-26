"""Dashboard aggregates: scoped to the caller, and agreeing with the frontend's rules.

Two properties, and the first is the one with teeth. These counts run over the whole
Support Ticket table, so if permission_query_conditions do not reach them the endpoint
reports other tenants' totals to anyone who calls it. permissions.py documents that
`get_all` and raw SQL bypass those conditions; this file is the proof that the aggregate
path does not.

The second is that the arithmetic matches lib/helpers.ts. `needs_attention` is a
disjunction of overlapping sets counted as four disjoint ones, which is exactly the kind
of decomposition that is right in the abstract and off by the overlap in practice — so it
is checked against a direct implementation over the same rows rather than by inspection.
"""
import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from inventive_helpdesk_backend.api import (
    _ACTIVE_STATUSES,
    _PENDING_CLIENT_STALE_DAYS,
    _RESOLVED_STATUSES,
    ticket_stats,
)

A_CLIENT, B_CLIENT = "_Stats Client A", "_Stats Client B"
POC_EMAIL = "_stats.poc@example.test"
MANAGER_EMAIL = "_stats.manager@example.test"


def _ensure(doctype, filters, payload):
    return frappe.db.get_value(doctype, filters) or frappe.get_doc(
        {"doctype": doctype, **payload}
    ).insert(ignore_permissions=True).name


def _user(email, roles, user_type="System User"):
    """Create a login holding `roles`.

    Staff take BOTH Support Team and Support Manager. Support Manager carries no DocPerm on
    Support Ticket of its own — install.py grants it "on top of Support Team" — so a
    manager holding only the manager role cannot read a ticket at all, and every query here
    would raise rather than return a scoped count.
    """
    if not frappe.db.exists("User", email):
        frappe.get_doc({
            "doctype": "User", "email": email, "first_name": "Stats",
            "user_type": user_type, "send_welcome_email": 0,
        }).insert(ignore_permissions=True)
    # Roles are ensured on every run, not only at creation. IntegrationTestCase leaves its
    # users behind, so a fixture that assigned roles inside the "if new" branch would keep
    # whatever the FIRST run happened to give them — and quietly test the wrong identity
    # ever after.
    doc = frappe.get_doc("User", email)
    missing = [r for r in roles if r not in {x.role for x in doc.roles}]
    if missing:
        doc.add_roles(*missing)
    return email


class TestTicketStats(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.flags.skip_ticket_ack = True

        cls.a = _ensure("Client", {"client_name": A_CLIENT},
                        {"client_name": A_CLIENT, "client_code": "STA"})
        cls.b = _ensure("Client", {"client_name": B_CLIENT},
                        {"client_name": B_CLIENT, "client_code": "STB"})
        cls.a_div = _ensure("Division", {"client": cls.a, "division_code": "SDA"},
                            {"client": cls.a, "division_name": "Stats A", "division_code": "SDA"})
        cls.b_div = _ensure("Division", {"client": cls.b, "division_code": "SDB"},
                            {"client": cls.b, "division_name": "Stats B", "division_code": "SDB"})

        cls.manager = _user(MANAGER_EMAIL, ["Support Team", "Support Manager"])
        cls.poc_user = _user(POC_EMAIL, ["Support Client"], user_type="Website User")
        _ensure("POC", {"email": POC_EMAIL},
                {"poc_name": "Stats POC", "email": POC_EMAIL, "client": cls.a,
                 "user": POC_EMAIL, "divisions": [{"division": cls.a_div}]})

        def ticket(client, division, **over):
            payload = {
                "doctype": "Support Ticket", "title": "stats fixture", "description": "x",
                "ticket_type": "Query", "priority": "Low", "status": "New",
                "client": client, "division": division,
            }
            payload.update(over)
            return frappe.get_doc(payload).insert(ignore_permissions=True)

        # Client A — the POC's scope. A deliberate spread across the rules.
        cls.a_tickets = [
            ticket(cls.a, cls.a_div, status="New"),
            ticket(cls.a, cls.a_div, status="New", sla_risk=1),          # New AND at risk: one ticket
            ticket(cls.a, cls.a_div, status="In Progress", sla_risk=1),  # at risk, other-active
            ticket(cls.a, cls.a_div, status="Pending Client"),
            ticket(cls.a, cls.a_div, status="Resolved"),
            ticket(cls.a, cls.a_div, status="Closed"),
        ]
        # Age the Pending Client one past the staleness threshold.
        stale = cls.a_tickets[3]
        stale.db_set("creation", add_days(now_datetime(), -(_PENDING_CLIENT_STALE_DAYS + 1)),
                     update_modified=False)

        # Client B — invisible to the POC. If any of these leak into their figures the
        # scoping tests fail, which is the point of having a second tenant at all.
        cls.b_tickets = [ticket(cls.b, cls.b_div, status="New") for _ in range(4)]
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        frappe.flags.skip_ticket_ack = False
        super().tearDownClass()

    def tearDown(self):
        frappe.set_user("Administrator")

    # ---- scoping ---------------------------------------------------------
    def test_a_portal_contact_never_sees_another_tenants_tickets_in_the_totals(self):
        frappe.set_user(self.poc_user)
        stats = ticket_stats()
        visible = len(frappe.get_list("Support Ticket", fields=["name"], limit=0))
        self.assertEqual(
            stats["counts"]["total"], visible,
            "the aggregate must count exactly what this caller can list — if it is higher, "
            "permission_query_conditions are not reaching the COUNT",
        )
        self.assertNotIn(B_CLIENT, stats["by_client"], "another client appeared in the breakdown")

    def test_every_breakdown_is_scoped_too(self):
        """Not just the headline number: a grouped query is a separate code path, and one
        that named other tenants' clients or agents would leak the directory even if the
        totals happened to be right."""
        frappe.set_user(self.poc_user)
        stats = ticket_stats()
        total = stats["counts"]["total"]
        for key in ("by_status", "by_priority", "by_type", "by_client", "by_assignee", "by_team"):
            with self.subTest(breakdown=key):
                self.assertLessEqual(
                    sum(stats[key].values()), total,
                    f"{key} counts more tickets than this caller can see",
                )

    def test_a_manager_sees_both_tenants(self):
        frappe.set_user(self.manager)
        stats = ticket_stats()
        self.assertIn(A_CLIENT, stats["by_client"])
        self.assertIn(B_CLIENT, stats["by_client"])
        self.assertGreaterEqual(stats["counts"]["total"], len(self.a_tickets) + len(self.b_tickets))

    # ---- arithmetic ------------------------------------------------------
    def _direct(self, rows):
        """helpers.needsAttention, implemented plainly, as the thing to agree with."""
        cutoff = add_days(now_datetime(), -_PENDING_CLIENT_STALE_DAYS)
        return sum(
            1
            for r in rows
            if r["status"] == "New"
            or (r["status"] == "Pending Client" and r["creation"] <= cutoff)
            or (r["sla_risk"] and r["status"] in _ACTIVE_STATUSES)
        )

    def test_needs_attention_matches_a_direct_count_without_double_counting(self):
        """The four disjoint terms must total the same as the disjunction they replace.

        The fixture includes a New ticket that is ALSO an SLA risk, which satisfies two of
        the three rules — a naive sum of three counts reports it twice and passes every
        test that only checks the number is plausible.
        """
        for user in (self.manager, self.poc_user):
            with self.subTest(user=user):
                frappe.set_user(user)
                rows = frappe.get_list(
                    "Support Ticket", fields=["status", "sla_risk", "creation"], limit=0
                )
                self.assertEqual(ticket_stats()["counts"]["needs_attention"], self._direct(rows))

    def test_active_and_resolved_partition_the_pipeline(self):
        frappe.set_user(self.manager)
        stats = ticket_stats()
        by_status = stats["by_status"]
        self.assertEqual(
            stats["counts"]["active"],
            sum(n for s, n in by_status.items() if s in _ACTIVE_STATUSES),
        )
        self.assertEqual(
            stats["counts"]["resolved"],
            sum(n for s, n in by_status.items() if s in _RESOLVED_STATUSES),
        )

    def test_unassigned_tickets_are_counted_as_awaiting_a_team(self):
        """The fixture routes nothing, so every open ticket is waiting on triage. to_member
        counts tickets that HAVE a team and no member, so it must be zero here — the two
        are disjoint, and a version that dropped the assignee condition would still pass a
        test that only looked at to_system."""
        frappe.set_user(self.manager)
        counts = ticket_stats()["counts"]
        self.assertEqual(counts["to_system"], counts["active"])
        self.assertEqual(counts["to_member"], 0)

    def test_the_trend_is_bounded_by_the_requested_window(self):
        frappe.set_user(self.manager)
        self.assertEqual(len(ticket_stats(trend_weeks=4)["trend"]), 4)
        self.assertEqual(len(ticket_stats(trend_weeks=12)["trend"]), 12)
        # Clamped rather than trusted: this is a whitelisted argument.
        self.assertEqual(len(ticket_stats(trend_weeks=9999)["trend"]), 52)
        self.assertEqual(len(ticket_stats(trend_weeks=0)["trend"]), 8)

    def test_trend_weeks_are_counted_from_the_newest_ticket(self):
        frappe.set_user(self.manager)
        trend = ticket_stats(trend_weeks=6)["trend"]
        self.assertTrue(all({"week", "created", "resolved"} <= set(b) for b in trend))
        self.assertTrue(
            any(b["created"] for b in trend),
            "the fixture's tickets were created now, so the window must contain them",
        )
