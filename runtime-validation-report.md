# Runtime Validation and Submission Readiness

**Generated:** 2026-10-30  
**Target:** `qd-assignment`  
**Scope:** Final integration audit; no Docker daemon or PaySwift sandbox credentials were available.

## Summary

| Check | Result | Evidence |
|---|---|---|
| Compose syntax/configuration | PASS | `docker compose config --quiet` exited 0; services resolved as `db`, `payswift`, `app`. |
| Full Compose/PostgreSQL startup | UNVERIFIED | Docker CLI exists, but `docker info` and `docker compose ps` could not connect to `//./pipe/dockerDesktopLinuxEngine` (“The system cannot find the file specified”). No local PostgreSQL executable was available. |
| FastAPI startup/health | PASS (local fallback) | Started Uvicorn against seeded SQLite; `GET http://127.0.0.1:8000/health` returned HTTP 200 with database connected. This does not verify PostgreSQL or Compose. |
| PaySwift health/transaction | BLOCKED | `GET http://127.0.0.1:8081/health` failed to connect. No transaction was attempted because the sandbox was unavailable. |
| Full pytest suite | PASS | `.venv\Scripts\python.exe -m pytest -q`: exit 0, **62 passed**. |
| Live-URL evaluation | FAIL / partial | `python evals\run.py http://127.0.0.1:8000 --payswift-url http://127.0.0.1:8081`: runner exit 1, **5/8 passed**. The three auto-pay scenarios failed because PaySwift was unreachable; the other five passed. |
| Operations authentication | PASS | Unauthenticated live POSTs to both approval and rejection returned HTTP 401; suite also covers authorized workflows. |
| Duplicate/concurrent message payments | PASS | Regression tests assert exactly one PaySwift call for both repeated and simultaneous identical message IDs: **2 passed**. |
| GitHub Actions | CONFIGURED, NOT RUN | Workflow declares an unfiltered `push` trigger and builds/starts PaySwift for CI evaluation. No GitHub Actions run was observed from this workspace. |
| AI-log provenance | DOES NOT MEET EXPORT REQUIREMENT | All 114 pre-existing JSON records parsed, but each declares `provider: fallback` and `model: deterministic-rule-fallback`. These are app-generated fallback interaction records, not verified raw exports from an AI platform. |
| Browser dashboard | UNVERIFIED | Node.js 22.12.0 is available. `npx --yes playwright install chromium` began a 197.5 MiB download and remained at 0% after more than two minutes; it was stopped. No browser test ran. |

## Changes made during audit

- Corrected the evaluation runner’s direct-script import path and made expected auto-pay results require successful payment evidence; when a PaySwift URL is supplied, it also verifies the payout exists in the provider ledger.
- Added a durable message reservation, same-process serialization for identical message IDs, and a deterministic message-derived PaySwift idempotency key. A duplicate still processing receives HTTP 409 with `Retry-After`, rather than risking a second payout.
- Added a per-rider database row lock around auto-pay eligibility checks to serialize the daily payment limit on PostgreSQL.
- Made Compose work without a local `.env`, passed configured application settings through to the container, documented the financial policy variables, and configured CI for all pushes with a PaySwift sandbox process.
- Replaced the short README with the requested concise overview and disclosed the AI-log provenance limitation.

## Required verdict

```yaml
environment:
  docker: UNAVAILABLE — docker info could not connect to //./pipe/dockerDesktopLinuxEngine
  node: AVAILABLE — v22.12.0
  playwright: UNAVAILABLE — Chromium download remained at 0% (>2 minutes); install stopped
  infra-tier: FALLBACK(embedded/in-memory) — local SQLite used for the live HTTP run; Docker daemon unavailable
  browser-tier: FALLBACK(framework-native HTTP) — Playwright installation stalled; live API validation used; dashboard rendering remains unverified
startup: PASS — Uvicorn with SQLite; GET /health returned 200, database connected
integration: PASS — exit_code: 0, passed: 62, failed: 0, skipped: 0, unit and in-process integration suite
e2e: FAIL — exit_code: 1, passed: 5, failed: 3, eight scenarios against http://127.0.0.1:8000; auto-pay requires unavailable PaySwift
overall: FAIL — real PostgreSQL/Compose and PaySwift execution were not verified; three live evaluation scenarios could not pay
```

**Submission blockers:** run Compose with Docker enabled, rerun the HTTP evaluation with PaySwift healthy (including a safe sandbox transaction), and replace or supplement `ai-logs/` with genuine raw AI-session exports. The CI workflow and its sandbox job were inspected but not executed here.
