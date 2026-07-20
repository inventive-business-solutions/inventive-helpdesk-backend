# Inventive Helpdesk

**Inventive Helpdesk** is an after-sales support and ticketing tool by Inventive Business Solutions. It provides support tickets, clients, divisions, POCs, products, team members and assignment groups, with role-based tenant isolation, inbound email intake and outbound client notifications.

> This is the backend Frappe (v16) application for **Inventive Helpdesk**. The internal Frappe app name is `inventive_helpdesk_backend` and the module is `Inventive Helpdesk`.

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/inventive-business-solutions/inventive-helpdesk-backend --branch master
bench install-app inventive_helpdesk_backend
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/inventive_helpdesk_backend
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
