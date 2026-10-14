# Employee Attendance & Analytics API

FastAPI + PyMongo implementation for the Employee Attendance & Analytics assignment. Python 3.11+ and MongoDB 6.0+ are required.

## Run locally

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in the environment, then start from this repository root:

```bash
uvicorn app.main:app --port 8000
```

The service creates its indexes idempotently at startup. `GET /health` returns 200 only when MongoDB responds to a ping. Interactive API docs are available at `/docs`.

## Implemented

- Employee creation and paginated listing.
- Punch-in and punch-out with IST attendance dates, overnight shifts, strict grace period, overtime, half-day and half-up work-hour rounding.
- Paginated attendance listing and regularization with an append-only audit trail and concurrent-update protection.
- Monthly employee analytics, department summary, competition-ranked late leaderboard, and daily department trend using MongoDB aggregation pipelines.
- `executionStats` explain endpoint for all five required query families.

## Verification note

The Python source compiles, the OpenAPI contract parses, all contract routes are present, and deterministic helper checks cover IST/overnight dates, late grace boundaries, overtime thresholds, half-up work-hour rounding, epoch truncation, and request validation. A live MongoDB instance was not available in the build environment, so database integration, parallel-request behaviour, aggregation results, and real execution plans must still be verified locally before submission. Review `REVIEW.md` and `DECISIONS.md` and make sure you can explain the implementation in a live walkthrough.
