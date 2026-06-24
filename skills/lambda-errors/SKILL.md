---
name: lambda-errors
description: >
  Diagnose AWS Lambda function failures — read-only. Surfaces a function's error
  rate, throttles, timeouts, concurrency limits, and recent function/config
  changes, plus recent error log lines. Use when the incident involves a
  Lambda-based app or component (elevated errors, throttling, timeouts, or a
  function that started failing after a deploy). Resolves the function from the
  app name when not given.
---

# Lambda errors investigation

Lambda functions fail quietly — errors surface as CloudWatch metrics, throttles
silently drop events, and timeouts leave only a log line. This skill pulls the
key signals in one pass so you can distinguish a deploy-broken function from a
concurrency-cap throttle from a downstream-induced timeout.

## When to use

- The incident's app is Lambda-based or a Lambda function is a critical path
  component.
- Symptoms: elevated error rate, alarm on `Errors` or `Throttles`, a function
  that "stopped working" after a deploy, timeouts on downstream calls, OOM
  `Runtime.ExitError` exits.

## Inputs (from the incident context packet)

| Env var | Required | Meaning |
|---|---|---|
| `RELAY_REGION` | yes | AWS region (e.g. `us-east-1`). |
| `RELAY_APP_NAME` | yes | App name; used to discover the function when `RELAY_FUNCTION_NAME` is not supplied. |
| `RELAY_FUNCTION_NAME` | no | Lambda function name or ARN. If absent, the probe lists all functions and matches on app name. |
| `RELAY_WINDOW_MINUTES` | no | Lookback window for metrics and logs (default 60). |

## Run

```bash
RELAY_REGION=... RELAY_APP_NAME=... [RELAY_FUNCTION_NAME=...] ./probe.sh
```

The probe prints these sections (each isolated — one failing never aborts the rest):

1. **Resolution** — which function it found and how.
2. **Function config** — Runtime, MemorySize, Timeout, LastModified, State,
   LastUpdateStatus, ReservedConcurrentExecutions, and Environment variable
   **key names only** (values are never printed — they may hold secrets).
3. **Invocation metrics** — CloudWatch `AWS/Lambda` metrics over the window:
   Errors (Sum), Throttles (Sum), Invocations (Sum), Duration (Maximum &
   Average), ConcurrentExecutions (Maximum). Error rate computed when
   Invocations > 0.
4. **Concurrency headroom** — account-level concurrent execution limit vs. the
   function's reserved concurrency vs. observed peak, to detect
   throttling-by-cap.
5. **Recent error logs** — `logs filter-log-events` on
   `/aws/lambda/<function>` for ERROR, Task timed out, Runtime.ExitError,
   Unhandled, Exception, and `errorMessage` patterns. Last ~20 matching lines
   with timestamps.

## How to interpret (raw output → hypotheses)

- **Duration Maximum at/near the configured Timeout + "Task timed out" log
  lines** → the function is hitting its timeout limit; hypothesis: a downstream
  dependency is slow, or the configured Timeout is too short for the current
  workload. Cross-check with `network-connectivity` or `database-connectivity`.
- **Throttles > 0 with ConcurrentExecutions at or near the reserved or account
  limit** → concurrency-cap throttling; hypothesis: reserved concurrency is too
  low for the load spike, or the account-level limit is close to exhaustion.
  Raising the reserved concurrency (or requesting a limit increase) should
  resolve throttles.
- **Errors spiking immediately after `LastModified` timestamp** → the new
  version is broken; hypothesis: a code regression, a missing environment
  variable, or a dependency incompatibility introduced by the latest deploy.
  Correlate with the `recent-changes` skill.
- **`State` != `Active` or `LastUpdateStatus` != `Successful`** → the function
  itself is in a bad state (failed deployment, pending VPC attachment, etc.);
  hypothesis: the deploy or config update did not complete cleanly. The
  `StateReason` / `LastUpdateStatusReason` fields name the specific failure.
- **`Runtime.ExitError` or "Runtime exited with error: signal: killed" in
  logs** → the runtime process was OOM-killed; hypothesis: MemorySize is too
  low for peak usage or there is a memory leak. Bump MemorySize and re-deploy.
- **Error rate > 0 with no Throttles, Duration well under Timeout, State
  Active** → application-level error (exception, bad input, downstream 4xx/5xx
  returning as Lambda error); read the error log lines for the specific
  exception and pivot to `cloudwatch-alarm-context` or
  `database-connectivity`.

Always present these as hypotheses with the supporting evidence line, never as
a confirmed root cause. The human decides.
