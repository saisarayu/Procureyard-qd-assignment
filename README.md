# QuickDrop Rider Payout Dispute Desk

QuickDrop investigates rider payout complaints against trip and payout records, explains outcomes, and routes risky or above-policy cases to operations. Payments are reconciled with the PaySwift provider ledger.

## Run

Run `docker compose up --build`; open the API at `http://localhost:8000` and operations dashboard at `/ops`. PostgreSQL, FastAPI, and the PaySwift sandbox start together. To run the live evaluation, use `python evals/run.py http://localhost:8000 --payswift-url http://localhost:8081`. The deterministic fallback works without an LLM key. Local operations credentials default to `ops` / `quickdrop2026`; change them outside local development.

## Architecture and decisions

FastAPI orchestrates complaint handling, PostgreSQL stores imported trip/payout and dispute records, and PaySwift is the authority for payment confirmation. Groq/Gemini may interpret English, Hindi, or Hinglish and draft explanations; deterministic Python code owns identity checks, calculations, policy limits, dispute windows, escalation, and payment requests. The ₹200 cap and seven-day window follow the assignment; imported records and provider responses are treated as source of truth. Durable message reservations and stable idempotency keys protect retries.

## Validation

Docker Compose services were healthy; migrations and CSV import succeeded. The full suite passed (**66 passed**). Live evaluation against a clean running PostgreSQL/FastAPI/PaySwift stack passed **8/8**: A ₹25, B ₹15, C ₹150 provider-confirmed payments; D ₹10 pending approval; E/F escalated; G outside-window; H clarification. The dashboard successfully approved D and rejected E with credentials; the ₹10 approval was verified in PaySwift. A separate INR 1 sandbox payout was provider-confirmed; same-key replay returned the same payout and the provider ledger contained one record.

## Limitations

`ai-logs/` contains app-generated deterministic fallback records, not genuine raw AI-platform session exports. GitHub Actions is configured for every push, but no remote CI run was observed. LLM interpretation is unavailable without a configured provider key.
