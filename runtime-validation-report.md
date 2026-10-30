# Runtime Validation and Submission Readiness

**Generated:** 2026-10-30  
**Target:** `qd-assignment`  
**Evaluation target:** `http://127.0.0.1:8000` with PaySwift at `http://127.0.0.1:8081`

## Summary

| Check | Result | Evidence |
|---|---|---|
| Docker Compose | PASS | `docker compose config --quiet` exited 0. `docker compose up --build -d` initially hit a transient BuildKit snapshot error; `docker compose build app` followed by `docker compose up -d` completed successfully. |
| PostgreSQL / migrations / import | PASS | PostgreSQL health check: accepting connections. `alembic current`: `0001_initial_schema (head)`. Imported rows: 40 riders, 3,304 trips, 3,222 payout lines. |
| FastAPI / PaySwift connectivity | PASS | `GET /health`: `{"status":"healthy","database":"connected","payswift":"connected","environment":"production"}`. PaySwift `GET /health`: HTTP 200, `{"status":"ok"}`. All three Compose services healthy. |
| Safe PaySwift payment and idempotency | PASS | Test rider `R_AUDIT_FINAL_20261030`, INR 1. First POST HTTP 201, payout `pout_a8a6e4e74a23`, processed. Same-key replay HTTP 200 returned the same payout ID. Provider ledger contained exactly one matching record. |
| Full Python test suite | PASS | `.venv\Scripts\python.exe -m pytest -q`: exit 0, **66 passed**. |
| Live evaluation A–H | PASS | `python evals/run.py http://127.0.0.1:8000 --payswift-url http://127.0.0.1:8081`: exit 0, **8/8** against the freshly seeded, running PostgreSQL/FastAPI/PaySwift stack. |
| Dashboard | PASS | Browser opened `/ops`; pending list and trace views rendered. R014 approval and R007 rejection were exercised with operations credentials; statuses updated and pending list refreshed. |
| Operations authentication | PASS | Unauthenticated POST to the approval endpoint and rejection endpoint each returned HTTP 401. |
| GitHub Actions | CONFIGURED, NOT OBSERVED | `.github/workflows/ci.yml` declares unfiltered `push:` and `pull_request` triggers. No GitHub Actions execution result was observed; do not treat as a CI pass. |
| AI session exports | LIMITATION | All **217** inspected `ai-logs/session_*.json` files parse as application-generated `provider: fallback`, `model: deterministic-rule-fallback` records. They are not genuine raw exports from an AI platform. |

## Scenario evidence

| Case | Expected outcome | Actual outcome | Payment amount | Provider verified | Escalation / operations | Result |
|---|---|---|---:|---|---|---|
| A — Surge underpayment, T926334 | Auto-pay | Auto-paid | INR 25 | Yes | No escalation | PASS |
| B — Surge underpayment, T604546 | Auto-pay | Auto-paid | INR 15 | Yes | No escalation | PASS |
| C — Missing daily incentive, 2026-09-21 | Auto-pay | Auto-paid | INR 150 | Yes | No escalation | PASS |
| D — Duplicate penalty, T637155 | Pending approval | Pending approval; later approved in dashboard | INR 10, provider payout verified after approval | Yes, after approval | Approval item created then approved | PASS |
| E — Identity mismatch, T840677 | Escalated | Escalated; later rejected in dashboard | None | N/A | Escalation item created then rejected with notes | PASS |
| F — Distance anomaly, T312538 | Escalated | Escalated | None | N/A | R031 pending; trace shows 5,000 km and auto-pay blocked | PASS |
| G — Outside seven-day window | Outside window | Outside window | None | N/A | No escalation | PASS |
| H — Unclear complaint | Clarification | Clarification | None | N/A | No escalation | PASS |

The evaluation ran before manual dashboard decisions against a clean database. The dashboard subsequently showed all three expected items (R031, R007, R014). Approving R014 returned HTTP 200 with PaySwift payout `pout_36e4390041eb`; a provider-ledger query confirmed exactly one processed INR 10 payout for R014. Rejecting R007 returned HTTP 200 and recorded the operator notes. The pending list then contained only R031. Separate unauthenticated approval and rejection requests each returned HTTP 401.

## Commands and exit codes

```powershell
docker compose config --quiet                         # exit 0
docker compose build app                              # exit 0
docker compose up -d                                  # exit 0; app/db/payswift healthy
.venv\Scripts\python.exe -m pytest -q                 # exit 0; 66 passed
.venv\Scripts\python.exe evals\run.py http://127.0.0.1:8000 --payswift-url http://127.0.0.1:8081  # exit 0; 8/8
```

An earlier `docker compose up --build -d` attempt failed with a BuildKit snapshot-cache error; a no-cache retry stalled while downloading Debian package indexes and was stopped. The normal cached `docker compose build app` retry and service startup then succeeded. No application source changes were made to work around that host build issue.

## Remaining limitations

- Genuine raw AI-provider session exports are unavailable; do not submit the fallback JSON records as such.
- The workflow is configured to run on every push, but a GitHub Actions run was not observed.
- LLM-backed interpretation requires a configured provider API key; deterministic fallback was used in this environment.

## Verdict

```yaml
environment:
  docker: AVAILABLE — Docker Engine 28.3.2; Compose services healthy
  node: AVAILABLE — v22.12.0
  playwright: AVAILABLE — integrated browser used against the live dashboard
  infra-tier: PRIMARY(Docker Compose) — PostgreSQL 16, FastAPI, and PaySwift sandbox
  browser-tier: PRIMARY(Playwright) — live dashboard list, traces, authentication, approval, rejection
startup: PASS — docker compose build app; docker compose up -d; /health reports database and PaySwift connected
integration: PASS — exit_code: 0, passed: 66, failed: 0, skipped: 0, full pytest suite
e2e: PASS — exit_code: 0, scenarios passed: 8/8, live service URL; browser approval/rejection also returned HTTP 200
overall: PASS WITH SUBMISSION LIMITATIONS — runtime and scenario checks pass; genuine AI exports and observed remote CI run remain unavailable
```
