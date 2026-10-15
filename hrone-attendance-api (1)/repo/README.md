# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or put it in a local .env (see .env.example)
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Indexes are created automatically at startup (idempotent). MongoDB 6.0+ is required (uses `$setWindowFields`, `$rank`, `$dateAdd`).

## Status
All endpoints in the contract are implemented: employees, punch-in/out, list, PATCH regularize, the four analytics endpoints and `/admin/explain/{endpoint}`.

## Tests
- `tests/test_rules_and_crud.py`: rule tests (R1-R5) and CRUD flows; runs without MongoDB (`pip install pytest httpx mongomock`).
- `tests/check_analytics.py`: seeds awkward data into a scratch DB and compares every analytics endpoint with a plain-Python calculation, and checks the explain plans (needs a real MongoDB).

## Notes
- Analytics read the stored derived values, as the data model asks. Integers (half-days, hundredths of an hour, 1/10000ths of a rate) are used inside the pipelines so the final half-up rounding is exact.
- Department trend generates one row per day with `$range` + `$unwind` (not a Python loop) and computes the 7-day average with `$setWindowFields`.
- Queries are hinted at their intended index where the data shape could otherwise tempt the planner into a collection scan.
