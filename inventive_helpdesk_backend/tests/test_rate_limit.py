"""Per-user rate limits on the mutating endpoints.

Two halves, because each misses what the other catches:

  - the MECHANISM, exercised directly. `_enforce_rate_limit` short-circuits under
    frappe.in_test (the counters live in redis and outlive a run, so a suite calling
    add_message dozens of times would start failing on its second run), so calling the
    endpoints would prove nothing. These tests drive the counter itself.

  - the WIRING, asserted structurally. A limit nothing is attached to is the failure mode
    that looks fine forever: every endpoint keeps working, and nobody notices the cap is
    gone until something abuses it. Same reasoning as test_manager_gate, which asserts the
    permission hook over the whole master list rather than trusting each call site.
"""
import frappe
from frappe.tests import IntegrationTestCase

from inventive_helpdesk_backend import api

# Endpoint -> the budget it must be spending from. Adding a mutating endpoint without a
# decision here is the thing this table exists to force.
_EXPECTED = {
    "add_message": "message",
    "add_note": "note",
    "upload_attachment": "attachment",
    "invite_poc": "invite",
    "invite_member": "invite",
}


class TestRateLimitWiring(IntegrationTestCase):
    def test_every_expected_endpoint_is_decorated_with_the_right_budget(self):
        """The decorator has to still be attached, and spending from the intended budget.

        Asserted on the explicit `_rate_limit_action` marker rather than on __wrapped__.
        That was the first version of this test and it was worthless: frappe.whitelist
        wraps every endpoint it registers, so __wrapped__ is present on all of them,
        rate limited or not — the test passed identically with the decorator removed.
        """
        for name, action in _EXPECTED.items():
            fn = getattr(api, name)
            self.assertEqual(
                getattr(fn, "_rate_limit_action", None),
                action,
                f"api.{name} should spend from the {action!r} budget — decorator missing or repointed",
            )

    def test_the_marker_is_absent_from_endpoints_that_have_no_limit(self):
        """Proves the check above can actually fail. A structural test that cannot
        distinguish a decorated function from a bare one is worse than no test: it reports
        the wiring as present forever."""
        for name in ("reopen", "claim_ticket", "me"):
            self.assertIsNone(
                getattr(getattr(api, name), "_rate_limit_action", None),
                f"api.{name} is not expected to be rate limited — update _EXPECTED if that changed",
            )

    def test_every_budget_named_by_an_endpoint_exists(self):
        """Guards the other direction: a decorator naming a budget that was renamed or
        removed would raise KeyError at call time, in production, on that endpoint only."""
        for name, action in _EXPECTED.items():
            self.assertIn(action, api._RATE_LIMITS, f"api.{name} spends from an unknown budget {action!r}")

    def test_limits_are_above_human_use(self):
        """A limit low enough to hit by working is worse than none: it trains people to
        treat the error as noise. Nothing here should be reachable by a person typing."""
        for action, (limit, seconds) in api._RATE_LIMITS.items():
            self.assertGreaterEqual(limit, 100, f"{action} limit is low enough to hit by hand")
            self.assertLessEqual(seconds, 24 * 3600, f"{action} window is longer than a working day")


class TestRateLimitMechanism(IntegrationTestCase):
    """Drives the counter directly, since the enforcement path no-ops in tests."""

    def setUp(self):
        self.action = "message"
        self.limit, _seconds = api._RATE_LIMITS[self.action]
        self.users = ("rl.one@example.test", "rl.two@example.test")
        for u in self.users:
            # Raw `delete`, not `delete_value`: the counter is written with a raw `incr`,
            # and frappe's *_value helpers prefix the site themselves — so the two families
            # address different keys and clearing with the wrong one leaks the fixture
            # between runs. Same distinction email._ack_key documents.
            frappe.cache().delete(api._rate_limit_key(self.action, u))

    def tearDown(self):
        for u in self.users:
            frappe.cache().delete(api._rate_limit_key(self.action, u))

    def _spend(self, user, n):
        """Consume n from `user`'s budget, returning the final count."""
        key = api._rate_limit_key(self.action, user)
        count = 0
        for _ in range(n):
            count = frappe.cache().incr(key)
        return count

    def test_the_key_is_scoped_to_site_user_and_action(self):
        """All three matter. Without the site, two benches sharing a redis share a budget;
        without the action, a burst of replies would lock out attachments too."""
        key = api._rate_limit_key("message", "someone@example.test")
        self.assertIn(frappe.local.site, key)
        self.assertIn("someone@example.test", key)
        self.assertIn("message", key)
        self.assertNotEqual(key, api._rate_limit_key("note", "someone@example.test"))
        self.assertNotEqual(key, api._rate_limit_key("message", "other@example.test"))

    def test_counting_stops_being_allowed_past_the_limit(self):
        at_limit = self._spend(self.users[0], self.limit)
        self.assertEqual(at_limit, self.limit, "the budget should be exactly spent")
        self.assertGreater(self._spend(self.users[0], 1), self.limit, "the next call is over budget")

    def test_one_user_cannot_spend_another_users_budget(self):
        """The whole reason this is keyed on the session user rather than the request IP:
        the support team shares one NAT address, so an IP budget is a team budget, and one
        agent working quickly would lock out the rest."""
        self._spend(self.users[0], self.limit)
        self.assertEqual(self._spend(self.users[1], 1), 1, "a second user starts with a full budget")

    def test_enforcement_is_skipped_in_tests(self):
        """Documents the exemption rather than leaving it as a surprise: without it, redis
        counters surviving between runs would fail the suite for reasons unrelated to the
        code under test."""
        self._spend(self.users[0], self.limit + 50)
        try:
            api._enforce_rate_limit(self.action)
        except frappe.RateLimitExceededError:
            self.fail("_enforce_rate_limit must no-op under frappe.in_test")
