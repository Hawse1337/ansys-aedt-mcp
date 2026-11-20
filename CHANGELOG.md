# Changelog

## 0.2.0 - 2026-11-20

- Update the pinned PyAEDT client from 1.9.0 to 1.10.0 (current stable,
  https://aedt.docs.pyansys.com/version/stable/index.html).
- Live-attach audit on AEDT 2026.1 with PyAEDT 1.10.0: ping and list_projects
  succeed through the attach-only guard, and the desktop release path still works.
- Remove the UTF-8 BOM from pyproject.toml so 	omllib/pytest can read it.
- Refresh the PyAEDT version statements in README, deployment and Maxwell documents.

## 0.1.0

- Internal pre-release: PyAEDT-backed MCP server, guarded solve workflow, evidence capture.
