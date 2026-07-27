"""Seed the Inventive Helpdesk demo data. Idempotent.

Creates demo users with fixed passwords, so it refuses to run unless the site has
developer_mode enabled.

Run: bench --site helpdesk.localhost execute inventive_helpdesk_backend.seed.run
"""
import datetime

import frappe

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def _date(s):
    """'13 July 2026' -> date; '—'/'' -> None."""
    if not s or s.strip() in ("—", "-"):
        return None
    d, mon, y = s.strip().split()
    return datetime.date(int(y), MONTHS[mon], int(d))


def _dt(s):
    """'10 July 2026, 9:14 AM' or '8 July 2026' -> datetime (9:00 default)."""
    if not s or s.strip() in ("—", "-"):
        return None
    s = s.strip()
    try:
        return datetime.datetime.strptime(s, "%d %B %Y, %I:%M %p")
    except ValueError:
        pass
    d = _date(s)
    return datetime.datetime.combine(d, datetime.time(9, 0)) if d else None


def _upsert(doctype, filters, values):
    name = frappe.db.exists(doctype, filters)
    if name:
        return name
    doc = frappe.get_doc({"doctype": doctype, **values})
    doc.insert(ignore_permissions=True)
    return doc.name


def _user(email, first, last, password, roles, website):
    if frappe.db.exists("User", email):
        u = frappe.get_doc("User", email)
    else:
        u = frappe.get_doc({
            "doctype": "User", "email": email, "first_name": first, "last_name": last,
            "send_welcome_email": 0,
            "user_type": "Website User" if website else "System User",
        })
        u.new_password = password
        u.insert(ignore_permissions=True)
    have = {r.role for r in u.roles}
    for r in roles:
        if r not in have:
            u.append("roles", {"role": r})
    u.enabled = 1
    u.save(ignore_permissions=True)
    return email


def _div(client, division_name):
    return frappe.db.get_value("Division", {"client": client, "division_name": division_name}, "name")


def run():
    # Demo data with fixed demo passwords — never run against a production site.
    if not frappe.conf.get("developer_mode"):
        frappe.throw("inventive_helpdesk_backend.seed.run creates demo users with fixed passwords — "
                     "it only runs on sites with developer_mode enabled.")

    # Don't fire acknowledgement emails for the seeded demo tickets.
    frappe.flags.skip_ticket_ack = True

    # ---- Products ----
    for p in ("EniMAX", "EniPRO"):
        _upsert("Product", {"product_name": p}, {"product_name": p})

    # ---- Clients ----
    _upsert("Client", {"client_name": "Thermax"},
            {"client_name": "Thermax", "client_code": "THX", "since": "2024-02-01"})
    _upsert("Client", {"client_name": "Praj"},
            {"client_name": "Praj", "client_code": "PRJ", "since": "2024-05-01"})

    # ---- Client products (engagements) ----
    # These used to be a `product` value on the Client row itself. That field is gone, and
    # an unknown key in the dict above would have been dropped in silence — leaving seeded
    # clients running nothing, which is not a shape the app can otherwise produce. No
    # divisions: the old single field meant "this client runs this product", full stop.
    for client, product in (("Thermax", "EniMAX"), ("Praj", "EniPRO")):
        _upsert("Client Product", {"client": client, "product": product},
                {"client": client, "product": product})

    # ---- Divisions ----
    for client, dname, code in [
        ("Thermax", "Heating", "HTG"), ("Thermax", "Enviro", "ENV"), ("Thermax", "IPG WWS", "WWS"),
        ("Praj", "Engineering", "ENG"), ("Praj", "BioProcess", "BIO"),
    ]:
        _upsert("Division", {"client": client, "division_code": code},
                {"client": client, "division_name": dname, "division_code": code})

    # ---- Portal users + POCs ----
    pocs = [
        ("R. Mehta", "r.mehta@thermax.com", "Thermax", "Heating", "Thermax@123", "R.", "Mehta"),
        ("S. Rao", "s.rao@thermax.com", "Thermax", "Enviro", "Thermax@123", "S.", "Rao"),
        ("A. Kulkarni", "a.kulkarni@praj.net", "Praj", "Engineering", "Praj@123", "A.", "Kulkarni"),
    ]
    for pname, email, client, dname, pw, first, last in pocs:
        _user(email, first, last, pw, ["Support Client"], website=True)
        _upsert("POC", {"email": email}, {
            "poc_name": pname, "email": email, "is_primary": 1,
            "client": client, "division": _div(client, dname), "user": email,
        })

    # ---- Admin / owner (Inventive staff) ----
    # Support Manager = the org-management tier (clients, POCs, members, teams). The
    # seeded team members below get only Support Team, so they land as ticket "agents".
    _user("admin@inventive.io", "Admin", "", "Admin@123",
          ["Support Team", "Support Manager", "System Manager"], website=False)

    # ---- Team Members (each a real Inventive staff login) ----
    # An "Active" member has signed in at least once; "Invited" has been provisioned but
    # not yet logged in — the on_login hook flips Invited -> Active on first sign-in, so
    # seeding an Invited member with a real user lets you demo that flip end-to-end.
    members = [
        ("Abhishek Bankar", "abhishek.bankar@inventive.io", "Software Engineer", "Active"),
        ("Kiran Jaware", "kiran.jaware@inventive.io", "Support Engineer", "Active"),
        ("Viraj Pangavhane", "viraj.pangavhane@inventive.io", "Structural Engineer", "Active"),
        ("Varad Hadawale", "varad.hadawale@inventive.io", "Structural Engineer", "Active"),
        ("Sanjana Jadhav", "sanjana.jadhav@inventive.io", "QA Engineer", "Invited"),
    ]
    for mname, email, title, status in members:
        first, _sep, last = mname.partition(" ")
        _user(email, first, last, "Inventive@123", ["Support Team"], website=False)
        name = _upsert("Team Member", {"member_name": mname},
                       {"member_name": mname, "email": email, "title": title, "status": status, "user": email})
        # Backfill the link on rows seeded before members became real logins.
        frappe.db.set_value("Team Member", name, "user", email)

    # ---- Groups ----
    for gname, gmembers in [
        ("Structural Team", ["Viraj Pangavhane", "Varad Hadawale"]),
        ("IT Team", ["Abhishek Bankar", "Kiran Jaware", "Sanjana Jadhav"]),
    ]:
        if not frappe.db.exists("Assignment Group", gname):
            frappe.get_doc({
                "doctype": "Assignment Group", "group_name": gname,
                "members": [{"member": m} for m in gmembers],
            }).insert(ignore_permissions=True)

    # ---- Tickets ----
    for t in TICKETS:
        if frappe.db.exists("Support Ticket", t["id"]):
            continue
        client = None if t["client"] in ("—", "") else t["client"]
        division = _div(client, t["div"]) if client and t["div"] not in ("—", "") else None
        assignee = None if t["assignee"] in ("Unassigned", "") else t["assignee"]
        doc = frappe.get_doc({
            "doctype": "Support Ticket",
            "name": t["id"],
            "title": t["title"],
            "ticket_type": t["type"],
            "priority": t["priority"],
            "status": t["status"],
            "client": client,
            "division": division,
            "raised_by": t["raisedBy"],
            "assignee": assignee,
            "assignment_group": t.get("group"),
            "due_date": _date(t["due"]),
            "sla_risk": 1 if t["slaRisk"] else 0,
            "description": t["desc"],
            "source": t.get("source") or "Portal",
            "from_email": t.get("fromEmail"),
            "conversation": [
                {"kind": m["kind"], "author": m["author"], "role": m["role"],
                 "message_on": _dt(m["tm"]), "body": m["body"]}
                for m in t.get("conversation", [])
            ],
            "notes": [
                {"author": n["author"], "note_on": _dt(n["tm"]), "body": n["body"]}
                for n in t.get("notes", [])
            ],
        })
        doc.flags.name_set = True  # respect our explicit name
        doc.insert(ignore_permissions=True)
        # backdate creation to the demo date for realistic ages
        created = _date(t["created"])
        if created:
            frappe.db.set_value("Support Ticket", t["id"], "creation",
                                datetime.datetime.combine(created, datetime.time(9, 0)),
                                update_modified=False)

    frappe.db.commit()
    print("Seed done.",
          "Tickets:", frappe.db.count("Support Ticket"),
          "Clients:", frappe.db.count("Client"),
          "POCs:", frappe.db.count("POC"),
          "Members:", frappe.db.count("Team Member"))


TICKETS = [
    {"id": "THX-HTG-0051", "type": "Query", "priority": "Medium", "status": "New",
     "title": "Line list export to Excel throws an error", "client": "Thermax", "div": "Heating",
     "raisedBy": "R. Mehta", "assignee": "Unassigned", "created": "13 July 2026", "due": "—",
     "slaRisk": False,
     "desc": "Hi team — when I try Export ▸ Line List ▸ Excel on the Unit-4 sheets I get a red error and nothing downloads. Screenshot attached. Thanks, Rajesh.",
     "source": "Email", "fromEmail": "r.mehta@thermax.com",
     "conversation": [], "notes": []},
    {"id": "INB-0007", "type": "Query", "priority": "Medium", "status": "New",
     "title": "Enquiry about your engineering software for our plant", "client": "—", "div": "—",
     "raisedBy": "procurement@newco.com", "assignee": "Unassigned", "created": "13 July 2026", "due": "—",
     "slaRisk": False,
     "desc": "Hello, we're evaluating your engineering software for our facility and would like a demo. Regards, Procurement — NewCo.",
     "source": "Email", "fromEmail": "procurement@newco.com",
     "conversation": [], "notes": []},
    {"id": "THX-HTG-0042", "type": "Bug", "priority": "Critical", "status": "In Progress",
     "title": "Valve symbols mis-detected on scanned drawings", "client": "Thermax", "div": "Heating",
     "raisedBy": "R. Mehta", "assignee": "Abhishek Bankar", "group": "IT Team",
     "created": "10 July 2026", "due": "13 July 2026", "slaRisk": True,
     "desc": "On rotated gate-valve symbols from scanned drawings, the detector tags them as check-valves. Reproducible on ~15% of the Unit-4 sheet set. Attached two sample exports.",
     "conversation": [
         {"kind": "client", "author": "R. Mehta", "role": "Client", "tm": "10 July 2026, 9:14 AM",
          "body": "This is blocking our review of Unit-4. Screenshots attached."},
         {"kind": "team", "author": "Abhishek Bankar", "role": "Team → Client", "tm": "10 July 2026, 11:40 AM",
          "body": "Thanks — reproduced on our side. Investigating the rotation handling, will share an ETA today."},
     ],
     "notes": [
         {"author": "Abhishek Bankar", "tm": "10 July 2026, 11:52 AM",
          "body": "Root cause looks like the OCR confidence threshold drops on symbols rotated >30°. Detector then falls back to nearest-template = check valve."},
         {"author": "Kiran Jaware", "tm": "11 July 2026, 4:20 PM",
          "body": "Fix in PR #214 — re-run template match at 4 rotations and keep max score. Needs QA on the Unit-4 set before we ship."},
     ]},
    {"id": "THX-HTG-0039", "type": "Improvement", "priority": "High", "status": "In Progress",
     "title": "Batch-process 50 drawings in one run", "client": "Thermax", "div": "Heating",
     "raisedBy": "R. Mehta", "assignee": "Abhishek Bankar", "group": "IT Team", "created": "8 July 2026", "due": "15 July 2026",
     "slaRisk": False,
     "desc": "Right now we upload sheets one at a time. For a full unit we need to queue ~50 drawings and get a combined line list.",
     "conversation": [{"kind": "client", "author": "R. Mehta", "role": "Client", "tm": "8 July 2026",
                       "body": "Single-sheet upload is slow for a full unit. Can we batch?"}],
     "notes": [{"author": "Abhishek Bankar", "tm": "9 July 2026",
                "body": "Feasible with the existing queue worker. Scoping a bulk-upload tray + progress view."}]},
    {"id": "THX-ENV-0031", "type": "Query", "priority": "Medium", "status": "Pending Client",
     "title": "How do I export the line list to Excel?", "client": "Thermax", "div": "Enviro",
     "raisedBy": "S. Rao", "assignee": "Kiran Jaware", "group": "IT Team", "created": "9 July 2026", "due": "16 July 2026",
     "slaRisk": False,
     "desc": "Need the generated line list in .xlsx for our internal review template.",
     "conversation": [{"kind": "team", "author": "Kiran Jaware", "role": "Team → Client", "tm": "9 July 2026",
                       "body": "You can use Export ▸ Line List ▸ Excel from the drawing toolbar. Which version are you on? Sent a short clip."}],
     "notes": []},
    {"id": "THX-WWS-0022", "type": "New Feature", "priority": "Medium", "status": "New",
     "title": "Auto-generate equipment list from the drawing", "client": "Thermax", "div": "IPG WWS",
     "raisedBy": "R. Mehta", "assignee": "Unassigned", "created": "12 July 2026", "due": "19 July 2026",
     "slaRisk": False,
     "desc": "Alongside the line list, produce an equipment schedule (tag, type, service) directly from recognised symbols.",
     "conversation": [], "notes": []},
    {"id": "THX-HTG-0035", "type": "Improvement", "priority": "Low", "status": "New",
     "title": "Dark mode for the drawing canvas", "client": "Thermax", "div": "Heating",
     "raisedBy": "R. Mehta", "assignee": "Unassigned", "group": "IT Team", "created": "12 July 2026", "due": "—",
     "slaRisk": False,
     "desc": "Reviewing drawings for long sessions is hard on the eyes. A dark canvas option would help.",
     "conversation": [], "notes": []},
    {"id": "PRJ-ENG-0017", "type": "Query", "priority": "Low", "status": "Pending Client",
     "title": "Which browsers are supported for the editor?", "client": "Praj", "div": "Engineering",
     "raisedBy": "A. Kulkarni", "assignee": "Kiran Jaware", "group": "IT Team", "created": "7 July 2026", "due": "14 July 2026",
     "slaRisk": True,
     "desc": "IT wants the supported browser matrix before wider rollout.",
     "conversation": [{"kind": "team", "author": "Kiran Jaware", "role": "Team → Client", "tm": "7 July 2026",
                       "body": "Shared the matrix (Chrome/Edge 120+, Firefox 121+). Let me know if Safari is required."}],
     "notes": []},
    {"id": "PRJ-BIO-0009", "type": "Bug", "priority": "High", "status": "Acknowledged",
     "title": "Tag numbers overlap on dense diagrams", "client": "Praj", "div": "BioProcess",
     "raisedBy": "A. Kulkarni", "assignee": "Unassigned", "created": "11 July 2026", "due": "14 July 2026",
     "slaRisk": False,
     "desc": "On high-density fermentation sheets, generated tag labels overlap and become unreadable.",
     "conversation": [{"kind": "client", "author": "A. Kulkarni", "role": "Client", "tm": "11 July 2026",
                       "body": "See attached — labels stack on top of each other near the reactor bank."}],
     "notes": []},
    {"id": "THX-ENV-0028", "type": "Bug", "priority": "Medium", "status": "Resolved",
     "title": "PDF upload fails over 20 MB", "client": "Thermax", "div": "Enviro",
     "raisedBy": "S. Rao", "assignee": "Kiran Jaware", "group": "IT Team", "created": "4 July 2026", "due": "8 July 2026",
     "slaRisk": False,
     "desc": "Large scanned PDFs time out during upload.",
     "conversation": [
         {"kind": "team", "author": "Kiran Jaware", "role": "Team → Client", "tm": "6 July 2026",
          "body": "Raised the upload limit to 50 MB and added chunked transfer. Please confirm on your side."},
         {"kind": "client", "author": "S. Rao", "role": "Client", "tm": "6 July 2026",
          "body": "Confirmed working, thank you."},
     ],
     "notes": [{"author": "Kiran Jaware", "tm": "5 July 2026",
                "body": "Nginx client_max_body_size was the cap. Bumped + chunked upload on the client."}]},
    {"id": "THX-WWS-0019", "type": "Query", "priority": "Medium", "status": "Closed",
     "title": "Can two engineers edit the same drawing?", "client": "Thermax", "div": "IPG WWS",
     "raisedBy": "R. Mehta", "assignee": "Abhishek Bankar", "group": "IT Team", "created": "2 July 2026", "due": "6 July 2026",
     "slaRisk": False,
     "desc": "Question about concurrent editing.",
     "conversation": [{"kind": "team", "author": "Abhishek Bankar", "role": "Team → Client", "tm": "3 July 2026",
                       "body": "Currently one editor at a time with a lock; live co-editing is on the roadmap."}],
     "notes": []},
    {"id": "PRJ-ENG-0014", "type": "New Feature", "priority": "Low", "status": "New",
     "title": "Bulk user import via CSV", "client": "Praj", "div": "Engineering",
     "raisedBy": "A. Kulkarni", "assignee": "Unassigned", "created": "12 July 2026", "due": "—",
     "slaRisk": False,
     "desc": "Onboard 40+ engineers at once from a CSV rather than one by one.",
     "conversation": [], "notes": []},
]
