# QuickDrop Rider Payout Dispute Desk

FastAPI service that investigates rider payout complaints against trip and payout records, explains outcomes, and routes risky or above-policy cases to operations. Money is reconciled against PaySwift; local database records are not treated as proof of provider payment.

## Run

Copy `.env.example` to `.env`, set an LLM key if desired, then run `docker compose up --build`. The stack exposes the API/dashboard on `http://localhost:8000` and PaySwift on `http://localhost:8081`. The deterministic fallback runs without an LLM key. The local sample operations credentials are `ops` / `quickdrop2026`; replace them outside local development. Run the public-interface evaluation with `python evals/run.py http://localhost:8000 --payswift-url http://localhost:8081`.

## Design and assumptions

The LLM (Groq or Gemini) interprets English/Hindi/Hinglish complaints and drafts explanations. Python code owns identity checks, trip and payout lookup, fare/incentive calculations, dispute windows, the ₹200 / one-payment-per-rider-per-day policy, audit traces, and PaySwift requests. Stable message-derived idempotency keys and a durable message reservation protect retries; operations approve/reject actions require HTTP Basic credentials. The policy window and payment cap follow the supplied brief; the included records and sandbox are treated as the source of truth. Without an LLM key, regex/rule fallback is used.

## Validation and limitations

Final local suite: **62 passed**. Live FastAPI/SQLite health: **200**, database connected. The URL-based evaluation reached the running service: **5/8 passed**; its three auto-pay cases correctly failed because PaySwift was unreachable. Approve and reject both returned **401** without credentials. Compose configuration validates, but Docker’s daemon was unavailable, so PostgreSQL, the full Compose stack, the bundled PaySwift process, and a real sandbox transaction were not run. See [`runtime-validation-report.md`](./runtime-validation-report.md) for evidence.

`ai-logs/` contains app-generated JSON interaction records; inspected records identify the deterministic fallback provider, not exports from an AI chat platform. They do not establish genuine raw-session-export provenance and should be replaced with actual tool exports before submission. Browser rendering and GitHub Actions execution were not verified in this environment.
