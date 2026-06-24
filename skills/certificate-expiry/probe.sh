#!/usr/bin/env bash
# certificate-expiry/probe.sh — READ-ONLY ACM / ALB / live-TLS certificate diagnostics.
#
# Uses only describe*/list* calls plus a read-only openssl handshake.
# Never mutates account state.
#
# Inputs (environment):
#   RELAY_REGION          required  AWS region
#   RELAY_APP_NAME        optional  app name (used to discover ALB/cert by name)
#   RELAY_ACM_CERT_ARN    optional  specific ACM cert ARN; else all certs are listed
#   RELAY_ALB_ARN         optional  ALB ARN (alternative: RELAY_ALB_NAME)
#   RELAY_ALB_NAME        optional  ALB name (alternative: RELAY_ALB_ARN)
#   RELAY_ENDPOINT        optional  hostname[:port] for live TLS handshake
#   RELAY_WINDOW_DAYS     optional  "expiring soon" threshold in days (default 30)
#
# Prints human-readable sections; each section is isolated so one failure
# (missing permission, not-found, missing openssl) prints a note and the probe continues.
set -uo pipefail

REGION="${RELAY_REGION:-}"
APP="${RELAY_APP_NAME:-}"
CERT_ARN="${RELAY_ACM_CERT_ARN:-}"
ALB_ARN="${RELAY_ALB_ARN:-}"
ALB_NAME="${RELAY_ALB_NAME:-}"
ENDPOINT="${RELAY_ENDPOINT:-}"
WINDOW_DAYS="${RELAY_WINDOW_DAYS:-30}"

if [ -z "${REGION}" ]; then
  echo "ERROR: RELAY_REGION is required." >&2
  exit 2
fi

AWS=(aws --region "${REGION}" --output json)

section() { printf '\n=== %s ===\n' "$1"; }
note()    { printf '  [note] %s\n' "$1"; }

# ---------------------------------------------------------------------------
# 1. Resolution
# ---------------------------------------------------------------------------
section "Resolution"

# Resolve ALB ARN if not given directly
if [ -z "${ALB_ARN}" ]; then
  SEARCH_NAME="${ALB_NAME:-${APP}}"
  if [ -n "${SEARCH_NAME}" ]; then
    note "RELAY_ALB_ARN not set; discovering ALB by name match on '${SEARCH_NAME}'."
    ALB_ARN="$(
      "${AWS[@]}" elbv2 describe-load-balancers 2>/dev/null \
        | python3 -c "
import sys, json
d = json.load(sys.stdin)
search = '${SEARCH_NAME}'.lower()
for lb in d.get('LoadBalancers', []):
    if search in lb.get('LoadBalancerName', '').lower():
        print(lb['LoadBalancerArn'])
        break
" 2>/dev/null
    )"
    if [ -z "${ALB_ARN}" ]; then
      note "No ALB name-matched '${SEARCH_NAME}'; ALB listener section will be skipped."
    fi
  else
    note "RELAY_ALB_ARN, RELAY_ALB_NAME, and RELAY_APP_NAME all unset; ALB listener section will be skipped."
  fi
fi

[ -n "${ALB_ARN}" ] && echo "  ALB: ${ALB_ARN##*/} (${ALB_ARN})"

# Cert ARN resolution note
if [ -n "${CERT_ARN}" ]; then
  echo "  ACM cert: ${CERT_ARN} (explicit)"
else
  note "RELAY_ACM_CERT_ARN not set; will list all ACM certs in region and flag issues."
fi

# Endpoint note
if [ -n "${ENDPOINT}" ]; then
  echo "  Live TLS endpoint: ${ENDPOINT}"
else
  note "RELAY_ENDPOINT not set; live TLS check will be skipped."
fi

echo "  Expiry window: ${WINDOW_DAYS} days"

# ---------------------------------------------------------------------------
# Helper: compute days until a notAfter string (ISO-8601 / RFC 2822 / openssl)
# Prints an integer (negative = already expired).
# ---------------------------------------------------------------------------
days_until() {
  python3 -c "
import sys
from datetime import datetime, timezone

raw = sys.argv[1].strip()
fmts = [
    '%Y-%m-%dT%H:%M:%S%z',
    '%Y-%m-%dT%H:%M:%SZ',
    '%b %d %H:%M:%S %Y %Z',   # openssl x509 format: Jan  1 00:00:00 2026 GMT
]
dt = None
for fmt in fmts:
    try:
        dt = datetime.strptime(raw, fmt)
        break
    except ValueError:
        pass
if dt is None:
    print('?')
    sys.exit(0)
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
now = datetime.now(timezone.utc)
print(int((dt - now).total_seconds() // 86400))
" "$1" 2>/dev/null
}

# ---------------------------------------------------------------------------
# 2. ACM inventory
# ---------------------------------------------------------------------------
section "ACM inventory"
{
  if [ -n "${CERT_ARN}" ]; then
    CERT_ARNS_TO_CHECK=("${CERT_ARN}")
  else
    mapfile -t CERT_ARNS_TO_CHECK < <(
      "${AWS[@]}" acm list-certificates \
        --query 'CertificateSummaryList[].CertificateArn' \
        --output text 2>/dev/null | tr '\t' '\n'
    )
    if [ "${#CERT_ARNS_TO_CHECK[@]}" -eq 0 ] || [ -z "${CERT_ARNS_TO_CHECK[0]}" ]; then
      note "acm list-certificates returned nothing (no certs in region, or permission denied)."
    else
      echo "  Found ${#CERT_ARNS_TO_CHECK[@]} cert(s) in region; showing expired/problematic and those expiring within ${WINDOW_DAYS} days."
    fi
  fi

  # Cap list scan to first 50 to avoid runaway API calls
  CAP=50
  COUNT=0
  declare -A ACM_DAYS   # cert ARN -> days-until
  declare -A ACM_STATUS # cert ARN -> Status

  for arn in "${CERT_ARNS_TO_CHECK[@]}"; do
    [ -z "${arn}" ] && continue
    (( COUNT++ )) || true
    [ "${COUNT}" -gt "${CAP}" ] && { note "Capped at ${CAP} certs; remaining skipped."; break; }

    CDESC="$("${AWS[@]}" acm describe-certificate --certificate-arn "${arn}" 2>/dev/null)"
    if [ -z "${CDESC}" ]; then
      note "describe-certificate returned nothing for ${arn##*/} (permission?)."
      continue
    fi

    python3 -c "
import sys, json
from datetime import datetime, timezone

d = json.load(sys.stdin)
c = d.get('Certificate', {})
arn   = c.get('CertificateArn', '')
short = arn.split('/')[-1] if arn else '?'
domain  = c.get('DomainName', '?')
status  = c.get('Status', '?')
not_after_raw = str(c.get('NotAfter', ''))
in_use  = c.get('InUseBy', [])
elig    = c.get('RenewalEligibility', '')

# Days until expiry
days_str = '?'
if not_after_raw and not_after_raw != 'None':
    fmts = ['%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ']
    dt = None
    for fmt in fmts:
        try:
            dt = datetime.strptime(not_after_raw, fmt)
            break
        except ValueError:
            pass
    if dt:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days_left = int((dt - datetime.now(timezone.utc)).total_seconds() // 86400)
        days_str  = str(days_left)

print('  ---')
print('  ARN           :', short)
print('  DomainName    :', domain)
print('  Status        :', status)
print('  NotAfter      :', not_after_raw)
print('  DaysUntilExpiry:', days_str)
print('  InUseBy       :', ', '.join(in_use) if in_use else '(none)')
print('  RenewalEligibility:', elig)

rs = c.get('RenewalSummary', {})
if rs:
    print('  RenewalSummary.Status   :', rs.get('RenewalStatus', ''))
    print('  RenewalSummary.Reason   :', rs.get('RenewalStatusReason', ''))

# Flags
window = int('${WINDOW_DAYS}')
flags = []
if status != 'ISSUED':
    flags.append('STATUS_NOT_ISSUED (' + status + ')')
if days_str != '?':
    d = int(days_str)
    if d < 0:
        flags.append('EXPIRED (' + str(abs(d)) + ' days ago)')
    elif d <= window:
        flags.append('EXPIRING_SOON (' + str(d) + ' days remaining)')
if flags:
    print('  *** FLAGS:', ', '.join(flags), '***')
" <<< "${CDESC}" 2>/dev/null || note "could not parse describe-certificate for ${arn##*/}."

    # Stash status + days for cross-reference in section 3
    STATUS_VAL="$(python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('Certificate',{}).get('Status','?'))" <<< "${CDESC}" 2>/dev/null)"
    DAYS_VAL="$(python3 -c "
import sys, json
from datetime import datetime, timezone
d = json.load(sys.stdin)
c = d.get('Certificate', {})
raw = str(c.get('NotAfter', ''))
if not raw or raw == 'None':
    print('?')
    sys.exit(0)
fmts = ['%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ']
dt = None
for fmt in fmts:
    try:
        dt = datetime.strptime(raw, fmt)
        break
    except ValueError:
        pass
if dt is None:
    print('?')
    sys.exit(0)
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
print(int((dt - datetime.now(timezone.utc)).total_seconds() // 86400))
" <<< "${CDESC}" 2>/dev/null)"
    ACM_DAYS["${arn}"]="${DAYS_VAL:-?}"
    ACM_STATUS["${arn}"]="${STATUS_VAL:-?}"
  done
} || note "ACM inventory section encountered an unexpected error."

# ---------------------------------------------------------------------------
# 3. ALB listener certs
# ---------------------------------------------------------------------------
section "ALB listener certs"
if [ -z "${ALB_ARN}" ]; then
  note "No ALB resolved; skipping this section."
else
  {
    LISTENERS="$("${AWS[@]}" elbv2 describe-listeners --load-balancer-arn "${ALB_ARN}" 2>/dev/null)"
    if [ -z "${LISTENERS}" ]; then
      note "describe-listeners returned nothing (permission, or no listeners?)."
    else
      HTTPS_LISTENERS="$(python3 -c "
import sys, json
d = json.load(sys.stdin)
for l in d.get('Listeners', []):
    if l.get('Port') == 443 or l.get('Protocol') == 'HTTPS':
        print(l.get('ListenerArn', ''))
" <<< "${LISTENERS}" 2>/dev/null)"

      if [ -z "${HTTPS_LISTENERS}" ]; then
        note "No HTTPS/443 listeners found on this ALB."
      else
        while IFS= read -r listener_arn; do
          [ -z "${listener_arn}" ] && continue
          echo "  Listener: ${listener_arn##*/}"

          # Default cert (from describe-listeners)
          python3 -c "
import sys, json
d = json.load(sys.stdin)
for l in d.get('Listeners', []):
    if l.get('ListenerArn') == '${listener_arn}':
        for cert in l.get('Certificates', []):
            print('    default cert ARN :', cert.get('CertificateArn', '?'))
" <<< "${LISTENERS}" 2>/dev/null || true

          # SNI certs via describe-listener-certificates
          LCERTS="$("${AWS[@]}" elbv2 describe-listener-certificates \
              --listener-arn "${listener_arn}" 2>/dev/null)"
          python3 -c "
import sys, json
d = json.load(sys.stdin)
certs = d.get('Certificates', [])
if not certs:
    print('    (no additional SNI certs)')
for cert in certs:
    is_default = cert.get('IsDefault', False)
    label = 'default' if is_default else 'SNI'
    print('    [%s] cert ARN: %s' % (label, cert.get('CertificateArn', '?')))
" <<< "${LCERTS}" 2>/dev/null || note "describe-listener-certificates failed for ${listener_arn##*/}."

          # Cross-reference ACM data collected in section 2
          ALL_LISTENER_CERT_ARNS="$(python3 -c "
import sys, json
seen = set()
for src in [sys.argv[1], sys.argv[2]]:
    try:
        d = json.loads(src)
    except Exception:
        continue
    for l in d.get('Listeners', []) + d.get('Certificates', []):
        arn = l.get('CertificateArn') or l.get('ListenerArn')
        if arn and arn not in seen:
            seen.add(arn)
            if 'certificate' in arn or 'certificate' in arn.lower():
                print(arn)
" "${LISTENERS}" "${LCERTS:-{}}" 2>/dev/null)"

          # Simpler: pull cert ARNs from both payloads
          {
            python3 -c "
import sys, json
for src in [sys.argv[1], sys.argv[2]]:
    try:
        d = json.loads(src)
    except Exception:
        continue
    for l in d.get('Listeners', []):
        if l.get('ListenerArn','') == sys.argv[3]:
            for cert in l.get('Certificates', []):
                a = cert.get('CertificateArn','')
                if a: print(a)
    for cert in d.get('Certificates', []):
        a = cert.get('CertificateArn','')
        if a: print(a)
" "${LISTENERS}" "${LCERTS:-{}}" "${listener_arn}" 2>/dev/null | sort -u
          } | while IFS= read -r cert_arn; do
            [ -z "${cert_arn}" ] && continue
            days="${ACM_DAYS[${cert_arn}]:-unknown}"
            status="${ACM_STATUS[${cert_arn}]:-unknown}"
            echo "    cross-ref ${cert_arn##*/}: Status=${status}, DaysUntilExpiry=${days}"
          done

        done <<< "${HTTPS_LISTENERS}"
      fi
    fi
  } || note "ALB listener certs section encountered an unexpected error."
fi

# ---------------------------------------------------------------------------
# 4. Live TLS check
# ---------------------------------------------------------------------------
section "Live TLS check"
if [ -z "${ENDPOINT}" ]; then
  note "RELAY_ENDPOINT not set; skipping live TLS handshake."
else
  {
    # Split host and port
    if printf '%s' "${ENDPOINT}" | grep -q ':'; then
      TLS_HOST="${ENDPOINT%%:*}"
      TLS_PORT="${ENDPOINT##*:}"
    else
      TLS_HOST="${ENDPOINT}"
      TLS_PORT="443"
    fi
    echo "  Connecting to ${TLS_HOST}:${TLS_PORT} ..."

    if ! command -v openssl &>/dev/null; then
      note "openssl not found in PATH; skipping live TLS check."
    else
      CERT_TEXT="$(echo | openssl s_client \
          -connect "${TLS_HOST}:${TLS_PORT}" \
          -servername "${TLS_HOST}" \
          2>/dev/null | openssl x509 -noout -dates -subject -issuer 2>/dev/null)"

      if [ -z "${CERT_TEXT}" ]; then
        note "openssl s_client returned no certificate (connection refused, timeout, or non-TLS service)."
      else
        echo "${CERT_TEXT}" | sed 's/^/  /'

        # Extract notAfter and compute days remaining
        NOT_AFTER_LINE="$(printf '%s' "${CERT_TEXT}" | grep '^notAfter=' | head -1)"
        NOT_AFTER_VAL="${NOT_AFTER_LINE#notAfter=}"

        if [ -n "${NOT_AFTER_VAL}" ]; then
          TLS_DAYS="$(python3 -c "
import sys
from datetime import datetime, timezone

raw = sys.argv[1].strip()
# openssl notAfter format: 'Jan  1 00:00:00 2026 GMT'
fmts = [
    '%b %d %H:%M:%S %Y %Z',
    '%b  %d %H:%M:%S %Y %Z',
    '%Y-%m-%dT%H:%M:%S%z',
    '%Y-%m-%dT%H:%M:%SZ',
]
dt = None
for fmt in fmts:
    try:
        dt = datetime.strptime(raw, fmt)
        break
    except ValueError:
        pass
if dt is None:
    print('?')
    sys.exit(0)
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
print(int((dt - datetime.now(timezone.utc)).total_seconds() // 86400))
" "${NOT_AFTER_VAL}" 2>/dev/null)"

          echo "  DaysUntilExpiry (live): ${TLS_DAYS:-?}"
          if [ -n "${TLS_DAYS}" ] && [ "${TLS_DAYS}" != "?" ]; then
            if [ "${TLS_DAYS}" -lt 0 ]; then
              echo "  *** FLAG: LIVE CERT IS EXPIRED (${TLS_DAYS#-} days ago) ***"
            elif [ "${TLS_DAYS}" -le "${WINDOW_DAYS}" ]; then
              echo "  *** FLAG: LIVE CERT EXPIRING SOON (${TLS_DAYS} days remaining) ***"
            fi
          fi
        else
          note "could not extract notAfter from openssl output."
        fi
      fi
    fi
  } || note "Live TLS check section encountered an unexpected error."
fi

echo
echo "Done. Findings are hypotheses — correlate cert expiry with incident timing and check ACM RenewalSummary for renewal failures."
