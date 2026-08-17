# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and Contributors
# See license.txt
"""Portal scope for client contacts, now that a contact holds a SET of divisions.

A division POC holds one, a client Lead holds the ones they oversee, and a freshly created
Lead holds none — which must mean no ticket access at all, not unscoped access. These tests
exist because getting that backwards leaks one client's tickets to another.
"""
from collections import defaultdict

import frappe
from frappe.tests import IntegrationTestCase

from inventive_helpdesk_backend import sender
from inventive_helpdesk_backend.api import set_contact_divisions

CLIENT = "_Test Scope Client"
OTHER_CLIENT = "_Test Scope Other"
LEAD_EMAIL = "_test.scope.lead@example.com"
POC_EMAIL = "_test.scope.poc@example.com"
OTHER_EMAIL = "_test.scope.other@example.com"


def _client(name, code):
    if not frappe.db.exists("Client", name):
        frappe.get_doc({"doctype": "Client", "client_name": name, "client_code": code}).insert(
            ignore_permissions=True
        )
    return name


def _division(client, dname, dcode):
    existing = frappe.db.get_value("Division", {"client": client, "division_code": dcode})
    if existing:
        return existing
    return frappe.get_doc({
        "doctype": "Division", "client": client, "division_name": dname, "division_code": dcode,
    }).insert(ignore_permissions=True).name


def _contact(email, client, divisions, is_lead=0, poc_name="Scope Contact"):
    if not frappe.db.exists("User", email):
        user = frappe.get_doc({
            "doctype": "User", "email": email, "first_name": "Scope", "last_name": "Contact",
            "user_type": "Website User", "send_welcome_email": 0,
        })
        user.append("roles", {"role": "Support Client"})
        user.insert(ignore_permissions=True)
    if frappe.db.exists("POC", email):
        frappe.delete_doc("POC", email, force=True, ignore_permissions=True)
    frappe.get_doc({
        "doctype": "POC", "poc_name": poc_name, "email": email, "client": client,
        "is_lead": is_lead, "user": email,
        "divisions": [{"division": d} for d in divisions],
    }).insert(ignore_permissions=True)
    return email


def _end_request():
    """Drop `frappe.local.request_cache`, standing in for the request boundary.

    permissions._poc is @request_cache'd — correct in production, where every HTTP request
    recomputes it, but a test process never ends a request, so a contact's scope would stay
    frozen at whatever the first test computed. Without this the suite reports leaks that
    cannot happen in a real request, and hides real ones behind a stale hit.

    Must be a defaultdict(dict), matching frappe/__init__.py:212 — the decorator writes
    `_cache[func][args_key]` and relies on the missing-key default, so a plain {} makes
    every cached function in the framework raise KeyError.
    """
    frappe.local.request_cache = defaultdict(dict)


def _ticket(client, division, title):
    return frappe.get_doc({
        "doctype": "Support Ticket", "title": title, "ticket_type": "Query",
        "priority": "Medium", "status": "New", "client": client, "division": division,
    }).insert(ignore_permissions=True)


class TestContactScope(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = _client(CLIENT, "ZSC")
        cls.other = _client(OTHER_CLIENT, "ZSO")
        cls.d1 = _division(cls.client, "One", "ZS1")
        cls.d2 = _division(cls.client, "Two", "ZS2")
        cls.d3 = _division(cls.client, "Three", "ZS3")
        cls.d_other = _division(cls.other, "Foreign", "ZSF")
        cls.t1 = _ticket(cls.client, cls.d1, "Scope one")
        cls.t2 = _ticket(cls.client, cls.d2, "Scope two")
        cls.t3 = _ticket(cls.client, cls.d3, "Scope three")
        cls.t_other = _ticket(cls.other, cls.d_other, "Foreign ticket")

    def tearDown(self):
        frappe.set_user("Administrator")
        super().tearDown()

    def _visible(self, user):
        _end_request()  # each read is a fresh request, as it would be over HTTP
        frappe.set_user(user)
        return {t.name for t in frappe.get_list("Support Ticket", fields=["name"], limit_page_length=0)}

    def test_contact_sees_only_the_divisions_it_holds(self):
        user = _contact(LEAD_EMAIL, self.client, [self.d1, self.d2], is_lead=1, poc_name="Scope Lead")
        seen = self._visible(user)
        self.assertIn(self.t1.name, seen)
        self.assertIn(self.t2.name, seen)
        self.assertNotIn(self.t3.name, seen, "a division they do not hold must stay invisible")

    def test_lead_with_no_divisions_sees_no_division_tickets(self):
        # How a Lead is created during client onboarding, before any division exists. An
        # empty scope must deny — the failure mode to avoid is it reading as "unscoped".
        #
        # Named for DIVISION tickets specifically: an empty scope is not literally "sees
        # nothing" any more, because a ticket carrying NO division is client-level and
        # visible to every contact of that client (see TestClientLevelAndProductScope). Every
        # fixture in this class has a division, so the exact-empty assertion below is still
        # the right one here — but the old name would turn a correct client-level ticket
        # added to these fixtures into a failure that looked like a regression.
        user = _contact(LEAD_EMAIL, self.client, [], is_lead=1, poc_name="Scope Lead")
        self.assertEqual(self._visible(user), set())

    def test_assigning_a_division_grants_exactly_that_division(self):
        user = _contact(LEAD_EMAIL, self.client, [], is_lead=1, poc_name="Scope Lead")
        self.assertEqual(self._visible(user), set())

        frappe.set_user("Administrator")
        set_contact_divisions(LEAD_EMAIL, [self.d3])

        seen = self._visible(user)
        self.assertEqual(seen, {self.t3.name})

    def test_removing_a_division_revokes_access(self):
        user = _contact(LEAD_EMAIL, self.client, [self.d1, self.d2], is_lead=1, poc_name="Scope Lead")
        self.assertIn(self.t1.name, self._visible(user))

        frappe.set_user("Administrator")
        set_contact_divisions(LEAD_EMAIL, [self.d2])  # d1 dropped

        seen = self._visible(user)
        self.assertNotIn(self.t1.name, seen, "a division removed in the UI must actually lose access")
        self.assertIn(self.t2.name, seen)

    def test_another_clients_ticket_is_invisible_by_list_and_by_direct_get(self):
        # permission_query_conditions covers list/report only, so the direct fetch is a
        # separate code path (ticket_has_permission) and needs its own assertion — a leak
        # could hide in either one alone.
        user = _contact(LEAD_EMAIL, self.client, [self.d1, self.d2, self.d3], is_lead=1)
        self.assertNotIn(self.t_other.name, self._visible(user))
        _end_request()
        frappe.set_user(user)
        self.assertFalse(frappe.get_doc("Support Ticket", self.t_other.name).has_permission("read"))

    def test_division_from_another_client_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            _contact(OTHER_EMAIL, self.client, [self.d_other])

    def test_same_division_twice_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            _contact(OTHER_EMAIL, self.client, [self.d1, self.d1])


class TestReplyAddressAfterPrimaryRetired(IntegrationTestCase):
    """`reply_address` used to address the division's is_primary contact. That flag is gone,
    so the fallback is by role — division POC first, then a Lead who oversees the division.
    It fails silently (no acknowledgement mail, no error), hence a test per branch."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = _client(CLIENT, "ZSC")
        cls.d1 = _division(cls.client, "One", "ZS1")

    def tearDown(self):
        frappe.set_user("Administrator")
        for email in (POC_EMAIL, LEAD_EMAIL):
            if frappe.db.exists("POC", email):
                frappe.delete_doc("POC", email, force=True, ignore_permissions=True)
        super().tearDown()

    def test_prefers_the_divisions_own_poc_over_a_lead(self):
        _contact(LEAD_EMAIL, self.client, [self.d1], is_lead=1, poc_name="A Lead")
        _contact(POC_EMAIL, self.client, [self.d1], is_lead=0, poc_name="A POC")
        ticket = frappe._dict({"division": self.d1, "from_email": None, "owner": "Administrator"})
        self.assertEqual(sender.reply_address(ticket), POC_EMAIL)

    def test_falls_back_to_a_lead_when_the_division_has_no_poc(self):
        _contact(LEAD_EMAIL, self.client, [self.d1], is_lead=1, poc_name="A Lead")
        ticket = frappe._dict({"division": self.d1, "from_email": None, "owner": "Administrator"})
        self.assertEqual(sender.reply_address(ticket), LEAD_EMAIL)

    def test_matches_the_named_raiser_ahead_of_either(self):
        _contact(POC_EMAIL, self.client, [self.d1], is_lead=0, poc_name="Named Raiser")
        _contact(LEAD_EMAIL, self.client, [self.d1], is_lead=1, poc_name="A Lead")
        ticket = frappe._dict({
            "division": self.d1, "from_email": None, "owner": "Administrator",
            "raised_by": "Named Raiser",
        })
        self.assertEqual(sender.reply_address(ticket), POC_EMAIL)

    def test_no_contact_on_the_division_yields_no_address(self):
        ticket = frappe._dict({"division": self.d1, "from_email": None, "owner": "Administrator"})
        self.assertIsNone(sender.reply_address(ticket))


class TestClientLevelAndProductScope(IntegrationTestCase):
    """Products a contact may see, and tickets carrying no division.

    Three people, from the requirement this was built for:

      A — a POC tagged into two divisions, each running its own product
      B — a Lead at a client with NO divisions, running two client-wide products
      C — a Lead at a client that HAS divisions, tagged into none of them

    The rule is one line for each concern. Tickets: `division IN (theirs)` OR the ticket
    carries no division and belongs to their client. Products: client-wide engagements, plus
    those covering a division they hold.

    C is the case worth writing down. Toggling every division off must leave them seeing no
    division tickets and no division products — but still the client-wide products, because
    those are attached to the company and not to any division. "Not scoped yet" and "scoped
    to everything" must never collapse into the same state: that failure looks exactly like
    the feature working.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = _client(CLIENT, "ZSC")
        cls.other = _client(OTHER_CLIENT, "ZSO")
        cls.d1 = _division(cls.client, "One", "ZS1")
        cls.d2 = _division(cls.client, "Two", "ZS2")
        cls.d3 = _division(cls.client, "Three", "ZS3")

    def setUp(self):
        _end_request()

    def tearDown(self):
        frappe.set_user("Administrator")
        _end_request()

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _product(name):
        if not frappe.db.exists("Product", name):
            frappe.get_doc({"doctype": "Product", "product_name": name}).insert(ignore_permissions=True)
        return name

    @classmethod
    def _engagement(cls, client, product, divisions):
        return frappe.get_doc({
            "doctype": "Client Product", "client": client, "product": cls._product(product),
            "divisions": [{"division": d} for d in divisions],
        }).insert(ignore_permissions=True)

    @staticmethod
    def _visible_products(user):
        frappe.set_user(user)
        _end_request()
        return {r.product for r in frappe.get_list("Client Product", fields=["product"], limit_page_length=0)}

    @staticmethod
    def _visible_tickets(user):
        frappe.set_user(user)
        _end_request()
        return {r.name for r in frappe.get_list("Support Ticket", fields=["name"], limit_page_length=0)}

    # ---- tickets ----------------------------------------------------------
    def test_a_contact_sees_their_divisions_tickets(self):
        mine = _ticket(self.client, self.d1, "ZS in my division")
        theirs = _ticket(self.client, self.d2, "ZS in another division")
        _contact(POC_EMAIL, self.client, [self.d1])
        seen = self._visible_tickets(POC_EMAIL)
        self.assertIn(mine.name, seen)
        self.assertNotIn(theirs.name, seen)

    def test_a_ticket_with_no_division_is_visible_to_every_contact_of_that_client(self):
        # Client-level: an emailed-in ticket nobody has scoped, or one raised by a contact
        # who holds no divisions. Under a divisions-only rule this was invisible to the
        # whole client side, including whoever raised it.
        t = _ticket(self.client, None, "ZS client level")
        _contact(POC_EMAIL, self.client, [self.d1])
        self.assertIn(t.name, self._visible_tickets(POC_EMAIL))

    def test_a_contact_with_no_divisions_sees_client_level_tickets_but_no_division_ones(self):
        client_level = _ticket(self.client, None, "ZS client level for C")
        scoped = _ticket(self.client, self.d1, "ZS division one")
        _contact(LEAD_EMAIL, self.client, [], is_lead=1)  # C: every division toggled off
        seen = self._visible_tickets(LEAD_EMAIL)
        self.assertIn(client_level.name, seen)
        self.assertNotIn(scoped.name, seen, "an unscoped lead must not see division tickets")

    def test_a_client_level_ticket_of_another_client_stays_invisible(self):
        foreign = _ticket(self.other, None, "ZS foreign client level")
        _contact(LEAD_EMAIL, self.client, [], is_lead=1)
        self.assertNotIn(foreign.name, self._visible_tickets(LEAD_EMAIL))

    # ---- products ---------------------------------------------------------
    def test_A_sees_the_products_of_the_divisions_they_hold(self):
        self._engagement(self.client, "_ZS Alpha", [self.d1])
        self._engagement(self.client, "_ZS Beta", [self.d2])
        _contact(POC_EMAIL, self.client, [self.d1, self.d2])
        self.assertEqual(self._visible_products(POC_EMAIL), {"_ZS Alpha", "_ZS Beta"})

    def test_A_does_not_see_a_product_of_a_division_they_do_not_hold(self):
        self._engagement(self.client, "_ZS Alpha", [self.d1])
        self._engagement(self.client, "_ZS Gamma", [self.d3])
        _contact(POC_EMAIL, self.client, [self.d1])
        seen = self._visible_products(POC_EMAIL)
        self.assertIn("_ZS Alpha", seen)
        self.assertNotIn("_ZS Gamma", seen)

    def test_B_and_C_see_client_wide_products_with_no_divisions_at_all(self):
        self._engagement(self.client, "_ZS Wide", [])  # client-wide
        self._engagement(self.client, "_ZS Alpha", [self.d1])
        _contact(LEAD_EMAIL, self.client, [], is_lead=1)
        seen = self._visible_products(LEAD_EMAIL)
        self.assertIn("_ZS Wide", seen, "client-wide products belong to the company, not a division")
        self.assertNotIn("_ZS Alpha", seen, "a division product must stay hidden from an unscoped lead")

    def test_a_client_wide_product_is_also_visible_to_a_division_contact(self):
        self._engagement(self.client, "_ZS Wide", [])
        _contact(POC_EMAIL, self.client, [self.d1])
        self.assertIn("_ZS Wide", self._visible_products(POC_EMAIL))

    def test_another_clients_engagements_are_never_visible(self):
        self._engagement(self.other, "_ZS Foreign", [])
        _contact(POC_EMAIL, self.client, [self.d1])
        self.assertNotIn("_ZS Foreign", self._visible_products(POC_EMAIL))

    def test_get_doc_is_refused_for_an_out_of_scope_engagement(self):
        # The half that query conditions do not cover. Without client_product_has_permission
        # a contact could read any engagement by name, including another client's terms.
        foreign = self._engagement(self.other, "_ZS Foreign Doc", [])
        hidden = self._engagement(self.client, "_ZS Gamma", [self.d3])
        _contact(POC_EMAIL, self.client, [self.d1])
        frappe.set_user(POC_EMAIL)
        _end_request()
        for name in (foreign.name, hidden.name):
            with self.assertRaises(frappe.PermissionError):
                frappe.get_doc("Client Product", name).check_permission("read")
