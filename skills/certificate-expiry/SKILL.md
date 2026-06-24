---
name: certificate-expiry
description: >
  Read-only; checks ACM certificate status/expiry, the certs bound to the app's
  ALB HTTPS listeners, and performs a live TLS handshake to read the served
  cert's expiry for an endpoint. Use for TLS handshake failures,
  "certificate expired/untrusted" errors, or HTTPS endpoints that suddenly fail.
---

# Certificate expiry investigation

TLS failures usually come down to one of three things: ACM hasn't renewed a
cert (or the validation DNS record was deleted), an ALB listener is pointing at
an expired or un-issued cert, or the cert the endpoint is actually serving
doesn't match what ACM thinks it issued. This skill surfaces all three without
you reconstructing the ACM → ALB → live-handshake call chain from memory.

## When to use

- TLS handshake failures (clients see `SSL_ERROR_RX_RECORD_TOO_LONG`,
  `ERR_CERT_DATE_INVALID`, `ERR_CERT_AUTHORITY_INVALID`, or similar).
- Alerts fired by a synthetic canary checking an HTTPS endpoint.
- An HTTPS endpoint that abruptly started returning connection errors or
  security warnings.
- ACM renewal notification emails (cert expiring in 45/30/7 days).

## Inputs (from the incident context packet)

| Env var | Required | Meaning |
|---|---|---|
| `RELAY_REGION` | yes | AWS region (e.g. `us-east-1`). |
| `RELAY_APP_NAME` | no | App name; used to match ALB/cert by name when ARNs are not supplied. |
| `RELAY_ACM_CERT_ARN` | no | Specific ACM certificate ARN to inspect. If absent, all certs in the region are listed and filtered for expiring/expired/non-ISSUED status. |
| `RELAY_ALB_ARN` | no | ALB ARN. If absent and `RELAY_ALB_NAME` is also absent, the probe tries to discover the ALB by app name. |
| `RELAY_ALB_NAME` | no | ALB name (alternative to `RELAY_ALB_ARN`). |
| `RELAY_ENDPOINT` | no | `hostname[:port]` for a live TLS handshake (e.g. `api.example.gov:443`). Port defaults to 443 if omitted. |
| `RELAY_WINDOW_DAYS` | no | "Expiring soon" threshold in days (default 30). Certs with fewer days remaining are flagged. |

## Run

```bash
RELAY_REGION=... [RELAY_APP_NAME=...] [RELAY_ACM_CERT_ARN=...] \
[RELAY_ALB_ARN=... or RELAY_ALB_NAME=...] [RELAY_ENDPOINT=...] \
[RELAY_WINDOW_DAYS=30] ./probe.sh
```

The probe prints these sections (each isolated — one failing never aborts the rest):

1. **Resolution** — which cert ARN / ALB / endpoint it is investigating and how each was discovered.
2. **ACM inventory** — `DomainName`, `Status`, `NotAfter`, days until expiry, `InUseBy`, and `RenewalEligibility`/`RenewalSummary`. Certs that are expired, expiring within `RELAY_WINDOW_DAYS`, or not in `ISSUED` status are flagged.
3. **ALB listener certs** — HTTPS (443) listener(s) on the resolved ALB; the default cert plus all SNI certs from `describe-listener-certificates`. Each cert is cross-referenced to the ACM expiry data collected in section 2.
4. **Live TLS check** — if `RELAY_ENDPOINT` is set, an `openssl s_client` handshake reads the leaf cert's `notAfter`, subject, and issuer, then reports days remaining. Skipped (with a note) if `openssl` is not installed or the connection fails.

## How to interpret (raw output → hypotheses)

- **ACM `Status: PENDING_VALIDATION` or `FAILED`** → ACM's managed renewal hit a
  snag. Most common cause: the DNS CNAME validation record was deleted (often
  accidentally during a DNS migration) or the cert was in `EMAIL` validation
  mode and the approval email was never clicked. Hypothesis: revalidation is
  required before ACM will issue a new cert.
- **ACM `NotAfter` already past, or days-until-expiry ≤ `RELAY_WINDOW_DAYS`** →
  the certificate has expired or is about to. If `Status` is still `ISSUED` but
  past expiry, ACM renewal may have silently failed (check `RenewalSummary`).
  This is a direct cause of TLS handshake failures for any client that enforces
  certificate validity.
- **ALB listener pointing at a cert with `Status != ISSUED`** → the ALB is
  configured to serve a cert that ACM has not (yet) issued; browsers will reject
  the connection outright. Direct cause of HTTPS errors.
- **ALB listener pointing at an expired cert** → even if ACM renewed, the
  listener may still reference the old cert ARN. Cross-reference the `InUseBy`
  field: if the renewed cert is not in `InUseBy` for this ALB, the listener was
  not updated.
- **Live served-cert `notAfter` mismatches ACM cert `notAfter`** → the ALB is
  serving a different cert than what ACM shows for that ARN (possible stale
  cache, wrong listener cert, or an out-of-band cert uploaded to IAM). The live
  TLS check subject/issuer will often identify whether it is an ACM cert or an
  external one.
- **Live TLS check connection refused / timeout** → the problem may not be the
  cert itself; the service may be down or the port unreachable. Pivot to
  `network-connectivity`.

Always present these as hypotheses with the evidence line that supports them,
never as a confirmed cause.
