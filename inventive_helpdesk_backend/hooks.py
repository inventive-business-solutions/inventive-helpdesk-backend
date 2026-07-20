app_name = "inventive_helpdesk_backend"
app_title = "Inventive Helpdesk"
app_publisher = "Inventive Business Solutions Pvt Ltd"
app_description = "After-sales support and ticketing for Inventive Business Solutions"
app_email = "support@inventive.io"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "inventive_helpdesk_backend",
# 		"logo": "/assets/inventive_helpdesk_backend/logo.png",
# 		"title": "Inventive Helpdesk",
# 		"route": "/inventive_helpdesk_backend",
# 		"has_permission": "inventive_helpdesk_backend.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/inventive_helpdesk_backend/css/inventive_helpdesk_backend.css"
# app_include_js = "/assets/inventive_helpdesk_backend/js/inventive_helpdesk_backend.js"

# include js, css files in header of web template
# web_include_css = "/assets/inventive_helpdesk_backend/css/inventive_helpdesk_backend.css"
# web_include_js = "/assets/inventive_helpdesk_backend/js/inventive_helpdesk_backend.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "inventive_helpdesk_backend/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "inventive_helpdesk_backend/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "inventive_helpdesk_backend.utils.jinja_methods",
# 	"filters": "inventive_helpdesk_backend.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "inventive_helpdesk_backend.install.before_install"
# after_install = "inventive_helpdesk_backend.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "inventive_helpdesk_backend.uninstall.before_uninstall"
# after_uninstall = "inventive_helpdesk_backend.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "inventive_helpdesk_backend.utils.before_app_install"
# after_app_install = "inventive_helpdesk_backend.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "inventive_helpdesk_backend.utils.before_app_uninstall"
# after_app_uninstall = "inventive_helpdesk_backend.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "inventive_helpdesk_backend.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"inventive_helpdesk_backend.tasks.all"
# 	],
# 	"daily": [
# 		"inventive_helpdesk_backend.tasks.daily"
# 	],
# 	"hourly": [
# 		"inventive_helpdesk_backend.tasks.hourly"
# 	],
# 	"weekly": [
# 		"inventive_helpdesk_backend.tasks.weekly"
# 	],
# 	"monthly": [
# 		"inventive_helpdesk_backend.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "inventive_helpdesk_backend.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "inventive_helpdesk_backend.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "inventive_helpdesk_backend.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["inventive_helpdesk_backend.utils.before_request"]
# after_request = ["inventive_helpdesk_backend.utils.after_request"]

# Job Events
# ----------
# before_job = ["inventive_helpdesk_backend.utils.before_job"]
# after_job = ["inventive_helpdesk_backend.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"inventive_helpdesk_backend.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []


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
    },
    "Support Ticket": {
        # Acknowledge every client-initiated ticket (emailed in or raised in the portal).
        "after_insert": "inventive_helpdesk_backend.email.send_ticket_ack",
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
