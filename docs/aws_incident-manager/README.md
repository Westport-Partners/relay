# AWS Systems Manager Incident Manager — documentation mirror

A local markdown mirror of the **AWS Systems Manager Incident Manager** user
guide, captured for historical reference. Incident Manager is end-of-life
(closed to new customers), so this preserves the full feature documentation of
the product Relay is built to replace — used to validate that Relay covers the
capabilities AWS abandoned.

- **Source:** https://docs.aws.amazon.com/incident-manager/latest/userguide/what-is-incident-manager.html
- **Captured:** 2026-06-21 (56 pages, fetched as `.md` from the AWS docs site)
- **Structure:** folder tree mirrors the user guide's table-of-contents
  hierarchy; a section that has sub-pages is a folder, with the section's own
  page as a sibling `.md` next to that folder.

## Pages most relevant to Relay

| Capability | Page |
|------------|------|
| Overview + incident lifecycle | `what-is-incident-manager.md`, `what-is-incident-manager/incident-lifecycle.md` |
| Contacts | `incident-response/contacts.md` |
| Escalation plans | `incident-response/escalation.md` |
| On-call schedules / rotations | `incident-response/incident-manager-on-call-schedule.md` (+ create/manage) |
| Response plans | `incident-response/response-plans.md` |
| Chat / ChatOps | `incident-response/chat.md` |
| Runbooks | `incident-response/runbooks.md` |
| Incident creation & tracking | `incident-creation.md`, `tracking.md` |
| Post-incident analysis (PIA) | `analysis.md` |
| Metrics & CloudTrail logging | `monitoring/` |
| Cross-account / cross-region | `incident-manager-cross-account-cross-region.md` |
| EOL migration guides (Jira/ServiceNow/PagerDuty/OpsCenter) | `incident-manager-availability-change/migration-guides/` |

The full page list is the directory tree under this folder.
