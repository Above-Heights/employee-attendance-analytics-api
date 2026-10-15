# Employee Attendance & Analytics API

FastAPI service for employee punches, manual corrections with audit history, and MongoDB-backed attendance analytics. The HTTP contract is in `openapi.yaml`; stored document shapes are in `DATA_MODEL.md`. All application code is in `app/main.py`.

## Run locally

Use Python 3.11+ and MongoDB 6.0+. From the repository root:

```text
python -m venv .venv
```

Activate `.venv` (`.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` on macOS/Linux), then run:

```text
python -m pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in the environment. For local development, copy `.env.example` to `.env` and edit it; environment variables take priority. Start MongoDB, then launch the API:

```text
uvicorn app.main:app --port 8000
```

The app creates its indexes on startup. `GET /health` returns 200 only when MongoDB responds.

The supplied `sample_seed.py` is optional. It **replaces all documents** in the selected database's `employees` and `attendance_logs` collections; use it only with a disposable local database.

## Test

```text
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Tests create a uniquely named temporary MongoDB database and remove it at the end. They cover validation, business-rule boundaries, concurrency, analytics, and explain plans.

`REVIEW.md` records starter defects and fixes. `DECISIONS.md` explains the index, concurrency, ranking, headcount, and scale choices.
