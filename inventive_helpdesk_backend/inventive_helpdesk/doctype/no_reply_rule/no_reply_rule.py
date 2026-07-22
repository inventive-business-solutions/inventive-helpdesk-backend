# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document


class NoReplyRule(Document):
	"""An operator override for no-reply detection.

	Layer 1 of the detection stack, and the reason layer 2 is allowed to be a blunt
	pattern list: when a built-in guess is wrong, a manager fixes it here instead of
	waiting for a release. Rules win in both directions — they can mark an address
	unmonitored that the patterns miss, and a Regex rule can be written to cover a
	convention the built-ins do not know about.
	"""

	def validate(self):
		self.pattern = (self.pattern or "").strip().lower()
		if not self.pattern:
			frappe.throw(_("A pattern is required"))
		if self.match_type == "Regex":
			# Compile now rather than swallowing the error at intake, where a broken
			# pattern would silently stop matching and nobody would know why.
			try:
				re.compile(self.pattern)
			except re.error as exc:
				frappe.throw(_("Not a valid regular expression: {0}").format(exc))

	def on_update(self):
		_invalidate()

	def on_trash(self):
		_invalidate()


def _invalidate():
	from inventive_helpdesk_backend import sender

	sender.clear_rule_cache()
