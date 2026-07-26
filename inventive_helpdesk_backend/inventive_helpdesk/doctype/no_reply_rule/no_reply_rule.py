# Copyright (c) 2026, Inventive Business Solutions Pvt Ltd and contributors
# For license information, please see license.txt

import re
import time

import frappe
from frappe import _
from frappe.model.document import Document

# An address pattern is a short thing. The cap is not a security control on its own — a
# catastrophic pattern fits easily in 200 characters — but it bounds the input to the
# timing probe below, which is.
MAX_PATTERN_LENGTH = 200

# Length of each adversarial probe run. Long enough that an exponential pattern blows the
# budget by orders of magnitude (2**24 steps), short enough that catching it costs
# milliseconds rather than the minutes it would take to let one finish.
PROBE_LENGTH = 24

# How long a pattern may take against a single probe. The built-in conventions in sender.py
# match in microseconds, so this is several orders of magnitude of headroom for anything
# sane, and still far below the point where a worker would notice.
PATTERN_TIME_BUDGET = 0.05


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
		if len(self.pattern) > MAX_PATTERN_LENGTH:
			frappe.throw(
				_("A pattern may be at most {0} characters.").format(MAX_PATTERN_LENGTH)
			)
		if self.match_type == "Regex":
			# Compile now rather than swallowing the error at intake, where a broken
			# pattern would silently stop matching and nobody would know why.
			try:
				compiled = re.compile(self.pattern)
			except re.error as exc:
				frappe.throw(_("Not a valid regular expression: {0}").format(exc))
			self._reject_catastrophic_pattern(compiled)

	def _reject_catastrophic_pattern(self, compiled):
		"""Refuse a pattern that can backtrack exponentially.

		Compiling proves a pattern is VALID, not that it terminates. `(a+)+$` compiles
		perfectly and takes exponential time on a non-matching string, and Python's `re`
		has no timeout to fall back on — so one rule like that stalls a worker on EVERY
		ticket save, because sender.classify runs from Support Ticket.before_save.

		Rather than parse the pattern looking for nested quantifiers — its own research
		problem, and easy to get subtly wrong — this runs it against short adversarial
		probes and rejects anything that has not finished promptly.

		The probe ALPHABET is taken from the pattern itself, which is the part that has to
		be right. A fixed "aaaa…" probe only backtracks on patterns built from `a`: the
		first version of this guard used one, and `(x+x+)+y$` sailed through it, was
		stored, and then ran against every subsequent ticket — turning an 8-test file into
		a five-minute one. The probe has to be made of the characters the pattern actually
		repeats, or it is not adversarial at all.

		This is a heuristic covering the classic shapes, not a proof of termination. It is
		the second line anyway: after the hooks.py gate, only a manager can write a rule.
		"""
		for probe in self._probes():
			start = time.perf_counter()
			try:
				compiled.search(probe)
			except Exception:
				# A pattern that raises on a plain ASCII string is not one to store.
				frappe.throw(_("That pattern could not be evaluated safely."))
			if time.perf_counter() - start > PATTERN_TIME_BUDGET:
				frappe.throw(
					_(
						"That pattern takes too long to evaluate and would slow down every "
						"ticket. Rewrite it without nested repetition such as (a+)+."
					),
					title=_("Pattern rejected"),
				)

	def _probes(self):
		"""Short strings likely to make THIS pattern backtrack.

		Each is a run of one character the pattern repeats, followed by a character that
		cannot match — the shape that forces a backtracking engine to try every split of
		the run. Capped at a handful of probes so validation stays fast on a pattern with
		many distinct literals.
		"""
		literals = []
		for ch in self.pattern:
			if ch.isalnum() and ch not in literals:
				literals.append(ch)
		# "a" covers a pattern written entirely from classes (\d+, \w+) with no literals.
		for ch in (literals[:4] or ["a"]):
			yield ch * PROBE_LENGTH + "!"
		yield "0" * PROBE_LENGTH + "!"

	def on_update(self):
		_invalidate()

	def on_trash(self):
		_invalidate()


def _invalidate():
	from inventive_helpdesk_backend import sender

	sender.clear_rule_cache()
