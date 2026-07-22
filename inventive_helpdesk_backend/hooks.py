# The generated commented-out hook catalogue that used to fill this file has been
# removed; it listed every hook Frappe supports, none of which this app used. For the
# full set see https://frappeframework.com/docs/v16/user/en/python-api/hooks or
# apps/frappe/frappe/hooks.py in the bench.

app_name = "inventive_helpdesk_backend"
app_title = "Inventive Helpdesk"
app_publisher = "Inventive Business Solutions Pvt Ltd"
app_description = "After-sales support and ticketing for Inventive Business Solutions"
app_email = "helpdesk@inventivebizsol.com"
app_license = "mit"

# --- Inventive Helpdesk: install/migrate ---
# Ship the app's roles in code so a fresh site has them before DocPerms load.
after_install = "inventive_helpdesk_backend.install.ensure_roles"
after_migrate = "inventive_helpdesk_backend.install.ensure_roles"

# --- Inventive Helpdesk: tenant isolation ---
permission_query_conditions = {
    "Support Ticket": "inventive_helpdesk_backend.permissions.ticket_query",
    "Client": "inventive_helpdesk_backend.permissions.client_query",
    "Division": "inventive_helpdesk_backend.permissions.division_query",
}
has_permission = {
    "Support Ticket": "inventive_helpdesk_backend.permissions.ticket_has_permission",
    # Masters: tenant isolation (where applicable) AND the manager-only write gate —
    # multiple hooks run and any one denying wins. Agents keep read; only managers write.
    "Client": [
        "inventive_helpdesk_backend.permissions.client_has_permission",
        "inventive_helpdesk_backend.permissions.manager_write_gate",
    ],
    "Division": [
        "inventive_helpdesk_backend.permissions.division_has_permission",
        "inventive_helpdesk_backend.permissions.manager_write_gate",
    ],
    "POC": "inventive_helpdesk_backend.permissions.manager_write_gate",
    "Product": "inventive_helpdesk_backend.permissions.manager_write_gate",
    "Team Member": "inventive_helpdesk_backend.permissions.manager_write_gate",
    "Assignment Group": "inventive_helpdesk_backend.permissions.manager_write_gate",
}

# --- Inventive Helpdesk: email intake + client notifications ---
doc_events = {
    "Communication": {
        "after_insert": "inventive_helpdesk_backend.email.on_communication",
        # Attachments need a SECOND hook, not a bigger after_insert. Frappe saves inbound
        # files only after the Communication is inserted (frappe/email/receive.py:741-748),
        # so at after_insert time there is nothing to find yet — the save that follows is
        # what fires this.
        "on_update": "inventive_helpdesk_backend.email.on_communication_update",
    },
    "Support Ticket": {
        "after_insert": [
            # Acknowledge every client-initiated ticket (emailed in or raised in the portal).
            "inventive_helpdesk_backend.email.send_ticket_ack",
            # Push a live "list changed" ping so a brand-new ticket appears in open list/board
            # views immediately, instead of waiting for the next 30s poll.
            "inventive_helpdesk_backend.realtime.publish_ticket_update",
        ],
        "on_update": [
            # Keep the client in the loop on client-facing status changes (Resolved / Pending Client).
            "inventive_helpdesk_backend.email.on_ticket_update",
            # Push a live nudge to everyone viewing the ticket (owner, team, collaborators).
            "inventive_helpdesk_backend.realtime.publish_ticket_update",
        ],
    },
}

# --- Inventive Helpdesk: staff onboarding ---
# Flip an invited Team Member to Active the first time they actually sign in.
on_login = "inventive_helpdesk_backend.api.activate_member_on_login"

# --- Inventive Helpdesk: inbound mail cadence ---
# Frappe ships this job at `0/10 * * * *`, which behind the scheduler tick means a customer
# waits 10-14 minutes for their email to become a ticket, and up to ~20 for the
# acknowledgement. Two minutes is the difference between "did that go through?" and not.
#
# It has to live HERE, not in the Desk UI. sync_jobs -> insert_single_event overwrites
# cron_format from hooks whenever they differ
# (frappe/core/doctype/scheduled_job_type/scheduled_job_type.py:270-275), and that runs on
# every `bench migrate` — so a value set by hand silently reverts on the next deploy, which
# is exactly when nobody is watching for it. Declared by this app, it survives, because our
# hooks are merged after frappe's own.
#
# The other half of the latency is `scheduler_tick_interval` + `scheduler_interval` in
# common_site_config.json, which are site config and cannot be shipped in the repo. See
# docs/RUNBOOK-production-mail.md.
scheduler_events = {
    "cron": {
        "*/2 * * * *": [
            "frappe.email.doctype.email_account.email_account.pull",
        ],
        # Mirror Email Queue outcomes onto Ticket Email Log. Email Queue writes its status
        # with frappe.db.set_value, which fires no doc events, so there is nothing to hook —
        # it has to be pulled.
        "*/5 * * * *": [
            "inventive_helpdesk_backend.email.reconcile_email_log",
        ],
    },
}
