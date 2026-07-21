"""Deployment health probe.

CI stamps the built commit into `build_sha.txt` inside the image (see the
"Inject build SHA into Containerfile" step in .github/workflows/ci.yml). This
endpoint reads it back so a deploy can be verified as actually running the build
that was just pushed — a plain "site returns 200" check cannot distinguish a new
container from the previous one still serving traffic.
"""

import os

import frappe


@frappe.whitelist(allow_guest=True, methods=["GET"])
def check():
	"""Liveness + build identity. Guest-callable and read-only: it exposes only the
	commit SHA of the running image, which CI compares against the commit it built."""
	return {"status": "ok", "build_sha": _build_sha()}


def _build_sha():
	"""The commit this image was built from, or "unknown" for a bench that was not
	built by CI (local dev, or an image predating the stamping step)."""
	try:
		path = os.path.join(frappe.get_app_path("inventive_helpdesk_backend"), "build_sha.txt")
		with open(path) as f:
			return f.read().strip() or "unknown"
	except OSError:
		return "unknown"
