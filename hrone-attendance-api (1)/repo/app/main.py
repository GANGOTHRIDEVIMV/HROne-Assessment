"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:   uvicorn app.main:app --port 8000
Env:   MONGO_URI, MONGO_DB   (a local .env is read for convenience; real environment variables win)

Layout of this single file:
  1. configuration & connection      5. employees endpoints
  2. time / rule helpers (R1-R5)     6. attendance endpoints (punch-in, punch-out, list, PATCH)
  3. serialisation helpers           7. analytics endpoints (3 aggregation pipelines + monthly)
  4. request models                  8. explain endpoint
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False (default): real environment variables win over .env
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration & connection
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes, never naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000  # contract: below MIN_MS looks like seconds
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = 4.5

# Index names are fixed so queries can be hinted at them and create_index stays idempotent.
IDX_EMP_UNIQUE = "emp_code_uq"
IDX_EMP_DEPT = "dept_joined"
IDX_LOG_UNIQUE = "emp_date_uq"
IDX_LOG_DATE = "date_emp"
IDX_LOG_STATUS = "status_date_emp"
IDX_LOG_PUNCH = "emp_punch_in"

_indexes_ready = False


def ensure_indexes() -> None:
    """Create every index we rely on. Idempotent: same spec + same name is a no-op."""
    global _indexes_ready
    employees.create_index([("emp_code", ASCENDING)], unique=True, name=IDX_EMP_UNIQUE)
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name=IDX_EMP_DEPT)
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name=IDX_LOG_UNIQUE)
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name=IDX_LOG_DATE)
    logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name=IDX_LOG_STATUS
    )
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name=IDX_LOG_PUNCH)
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep the process alive; /health reports 503 and retries
        log.warning("index creation failed at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.exception("database error")
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Time and business-rule helpers (R1 - R5)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def dt_to_ms(dt: datetime) -> int:
    return (aware(dt) - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight-shift punch-in earlier than shift_end
    belongs to the shift that started the previous day."""
    ist = aware(punch_in).astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < hhmm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_at(date_str: str, shift_start: str) -> datetime:
    return datetime.combine(date.fromisoformat(date_str), hhmm(shift_start), tzinfo=IST)


def shift_end_at(date_str: str, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(date.fromisoformat(date_str), hhmm(shift_end), tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then floor minutes since shift start."""
    seconds = int((aware(punch_in) - shift_start_at(date_str, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift end, only if at least 30."""
    seconds = int((aware(punch_out) - shift_end_at(date_str, shift_start, shift_end)).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals HALF-UP (Decimal, not float round())."""
    seconds = int((aware(punch_out) - aware(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict, date_str: str) -> dict:
    """All derived fields for a record, from the final status/times (R2-R5)."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in PRESENCE and punch_in is not None:
        out["late_minutes"] = compute_late_minutes(punch_in, date_str, emp["shift_start"])
        if punch_out is not None:
            hours = compute_work_hours(punch_in, punch_out)
            out["work_hours"] = hours
            out["overtime_minutes"] = compute_overtime(punch_out, date_str, emp["shift_start"], emp["shift_end"])
            out["half_day"] = hours < HALF_DAY_HOURS  # R5 uses the rounded value
    return out


def round_half_up(value: Any, places: int) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def div_half_up(numerator: int, denominator: int, places: int) -> float:
    q = Decimal(1).scaleb(-places)
    return float((Decimal(numerator) / Decimal(denominator)).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# 3. Serialisation and validation helpers
# --------------------------------------------------------------------------- #
def unprocessable(loc: str, msg: str):
    """Raise a 422 with FastAPI's standard validation body."""
    raise RequestValidationError([{"type": "value_error", "loc": ("query", loc), "msg": msg, "input": None}])


def parse_date(value: str, loc: str) -> date:
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        unprocessable(loc, "must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        unprocessable(loc, "not a valid calendar date")


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}"


def count_weekdays(start: str, end: str) -> int:
    d, stop, n = date.fromisoformat(start), date.fromisoformat(end), 0
    while d <= stop:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def _api_value(v: Any) -> Any:
    return dt_to_ms(v) if isinstance(v, datetime) else v


def history_to_api(entry: dict) -> dict:
    return {
        "at": _api_value(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            k: {"from": _api_value(v.get("from")), "to": _api_value(v.get("to"))}
            for k, v in (entry.get("changes") or {}).items()
        },
    }


def record_to_api(doc: dict) -> dict:
    """Stored attendance document -> API shape (no _id; instants as epoch ms; defaults for legacy gaps)."""
    pin, pout = doc.get("punch_in"), doc.get("punch_out")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(pin) if pin else None,
        "punch_out": dt_to_ms(pout) if pout else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_to_api(h) for h in (doc.get("history") or [])],
    }


def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]) if doc.get("created_at") else None,
    }


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[StrictInt, Field(ge=MIN_MS, le=MAX_MS)]  # strict: no floats / strings / bools
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if len(v) != 10 or v[4] != "-" or v[7] != "-":
            raise ValueError("must be YYYY-MM-DD")
        date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class PunchInIn(PunchOutIn):
    status: PresenceStatus = "PRESENT"


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


# --------------------------------------------------------------------------- #
# 5. Health and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:  # uniqueness is enforced by the unique index, so concurrent creates give one 201, rest 409
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = employees.count_documents(query)
    cursor = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_to_api(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 6. Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(body.status, ts, None, emp, day),
        "history": [],
    }
    try:  # no check-then-insert: the unique (emp_code, date) index decides the winner atomically
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # most recent record that started at or before the punch-out (works across midnight)
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = aware(rec["punch_in"])
    if ts <= pin or ts > pin + timedelta(hours=24):
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("body", "punched_at"), "msg": "must be after punch_in and within 24h", "input": None}]
        )
    fields = {"punch_out": ts, **derive(rec["status"], pin, ts, emp, rec["date"])}
    # the `punch_out: None` filter makes the close atomic: of two racing punch-outs only one matches
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, {"$set": fields}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_to_api(updated)


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    start = parse_date(date_from, "date_from") if date_from else None
    end = parse_date(date_to, "date_to") if date_to else None
    if start and end and start > end:
        unprocessable("date_from", "date_from must not be after date_to")
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = logs.count_documents(q) if q else logs.estimated_document_count()
    cursor = logs.find(q, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_to_api(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def plan_regularization(emp: dict, rec: dict, body: RegularizeIn):
    """Validate the correction against the stored record and return (set_fields, history_entry)."""
    sent = body.model_fields_set
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }

    def bad(msg: str):
        raise RequestValidationError([{"type": "value_error", "loc": ("body",), "msg": msg, "input": None}])

    status = body.status if "status" in sent else old["status"]
    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in sent or "punch_out" in sent:
            bad(f"{status} clears punch times; do not supply punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if "punch_in" in sent else old["punch_in"]
        new_out = ms_to_dt(body.punch_out) if "punch_out" in sent else old["punch_out"]
        if new_in is None:
            bad("a presence status requires a punch_in")
        new_in = aware(new_in)
        new_out = aware(new_out) if new_out is not None else None
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            bad("punch_in must stay on the record's attendance date")
        if new_out is not None and not (new_in < new_out <= new_in + timedelta(hours=24)):
            bad("punch_out must be after punch_in and within 24 hours")

    def same_instant(a, b):
        return (a is None and b is None) or (a is not None and b is not None and dt_to_ms(a) == dt_to_ms(b))

    if status == old["status"] and same_instant(new_in, old["punch_in"]) and same_instant(new_out, old["punch_out"]):
        bad("request changes nothing")

    new = {"status": status, "punch_in": new_in, "punch_out": new_out, **derive(status, new_in, new_out, emp, rec["date"])}
    changes = {}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        o, n = old[key], new[key]
        differs = not same_instant(o, n) if key in ("punch_in", "punch_out") else o != n
        if differs:
            changes[key] = {"from": o, "to": n}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    return new, entry


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date")
    emp = get_employee(emp_code)
    for _ in range(5):  # optimistic concurrency: re-read and retry if somebody changed the record under us
        rec = logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        fields, entry = plan_regularization(emp, rec, body)
        guard = {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
        }
        res = logs.update_one(guard, {"$set": fields, "$push": {"history": entry}})
        if res.matched_count == 1:
            return record_to_api(logs.find_one({"_id": rec["_id"]}))
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# 7. Analytics (aggregation pipelines)
# --------------------------------------------------------------------------- #
Month = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]

_PRES = list(PRESENCE)
_WEEKDAYS = [2, 3, 4, 5, 6]  # $dayOfWeek: 1 = Sunday ... 7 = Saturday


def _log_accumulators(p: str) -> dict:
    """$group accumulators over one log document at path prefix `p` ("$" for logs, "$logs." after a $lookup).
    Everything is kept in integers (half-days, hundredths of an hour) so the final rounding is exact."""
    status = {"$ifNull": [f"{p}status", ""]}
    present = {"$in": [status, _PRES]}
    weekday = {
        "$in": [
            {"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": [f"{p}date", "1970-01-01"]}}}},
            _WEEKDAYS,
        ]
    }
    half = {"$cond": [{"$eq": [{"$ifNull": [f"{p}half_day", False]}, True]}, 1, 2]}
    has_hours = {"$and": [present, {"$isNumber": f"{p}work_hours"}]}
    late = {"$ifNull": [f"{p}late_minutes", 0]}
    return {
        "present_half": {"$sum": {"$cond": [{"$and": [present, weekday]}, half, 0]}},
        "hours_cents": {
            "$sum": {"$cond": [has_hours, {"$toInt": {"$round": [{"$multiply": [f"{p}work_hours", 100]}, 0]}}, 0]}
        },
        "hours_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        "late_total": {"$sum": {"$cond": [{"$gt": [late, 0]}, late, 0]}},
        "overtime_total": {"$sum": {"$ifNull": [f"{p}overtime_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": [status, "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": [status, "ON_DUTY"]}, 1, 0]}},
    }


def employee_monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": None, **_log_accumulators("$")}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = get_employee(emp_code)
    start, end = month_bounds(month)
    agg = next(iter(logs.aggregate(employee_monthly_pipeline(emp_code, start, end), hint=IDX_LOG_UNIQUE)), {})
    working_days = count_weekdays(max(start, emp["joined_on"]), end) if emp["joined_on"] <= end else 0
    present_half = agg.get("present_half", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_half / 2,
        "leave_days": agg.get("leave_count", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("late_total", 0),
        "total_overtime_minutes": agg.get("overtime_total", 0),
        # present_days / working_days * 100  ==  present_half * 50 / working_days  (exact integer maths)
        "attendance_pct": div_half_up(present_half * 50, working_days, 2) if working_days else None,
    }


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    match: dict = {"joined_on": {"$lte": end}}  # R9: joined on or before month end, logs or not
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {  # uses the unique (emp_code, date) index for every employee
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}],
                "as": "logs",
            }
        },
        # preserveNullAndEmptyArrays keeps employees with zero logs, so they still count in headcount
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": {"department": "$department", "emp_code": "$emp_code"}, **_log_accumulators("$logs.")}},
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},  # exactly one row per employee after the first $group
                **{k: {"$sum": f"${k}"} for k in _log_accumulators("$logs.")},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    start, end = month_bounds(month)
    rows = employees.aggregate(department_summary_pipeline(start, end, department), hint=IDX_EMP_DEPT)
    items = []
    for r in rows:
        items.append(
            {
                "department": r["_id"],
                "headcount": r["headcount"],
                "present_days": r["present_half"] / 2,
                "avg_work_hours": div_half_up(r["hours_cents"], r["hours_n"] * 100, 2) if r["hours_n"] else None,
                "late_count": r["late_count"],
                "total_late_minutes": r["late_total"],
                "leave_count": r["leave_count"],
                "on_duty_count": r["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


def late_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # no matching employee -> dropped (logs of unknown employees are ignored)
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties at the cutoff are kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = month_bounds(month)
    rows = logs.aggregate(late_leaderboard_pipeline(start, end, limit, department), hint=IDX_LOG_DATE)
    return {"month": month, "items": list(rows)}


def department_trend_pipeline(department: str, start: date, days: int) -> list:
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    present_half = {
        "$sum": {
            "$cond": [
                {"$in": ["$status", _PRES]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]},
                0,
            ]
        }
    }
    late = {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
    window = {"documents": [-6, 0]}  # current row + up to 6 before it, never before `from`
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "joins": {"$push": "$joined_on"}, "codes": {"$push": "$emp_code"}}},
        # gap filling: one row per calendar day, generated in the database ($range), weekends included
        {"$project": {"_id": 0, "joins": 1, "codes": 1, "offset": {"$range": [0, days]}}},
        {"$unwind": "$offset"},
        {"$addFields": {"day": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$offset"}}}},
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
                "dow": {"$dayOfWeek": "$day"},
            }
        },
        {
            "$addFields": {  # R9 per day
                "headcount": {"$size": {"$filter": {"input": "$joins", "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}}
            }
        },
        {
            "$lookup": {  # that day's records of this department's employees (date index, then emp_code filter)
                "from": "attendance_logs",
                "localField": "date",
                "foreignField": "date",
                "let": {"codes": "$codes"},
                "pipeline": [
                    {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
                    {"$group": {"_id": None, "present_half": present_half, "late_count": late}},
                ],
                "as": "agg",
            }
        },
        {"$addFields": {"agg": {"$ifNull": [{"$first": "$agg"}, {"present_half": 0, "late_count": 0}]}}},
        {
            "$addFields": {
                "is_working_day": {"$in": ["$dow", _WEEKDAYS]},
                "present_half": "$agg.present_half",
                "late_count": "$agg.late_count",
            }
        },
        {
            "$addFields": {  # rate in 1/10000ths, half-up, in exact integer maths: floor((10000*p + h) / (2*h))
                "rate_n": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$toInt": {
                                "$floor": {
                                    "$divide": [
                                        {"$add": [{"$multiply": [10000, "$present_half"]}, "$headcount"]},
                                        {"$multiply": [2, "$headcount"]},
                                    ]
                                }
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {"$addFields": {"rate_ind": {"$cond": [{"$isNumber": "$rate_n"}, 1, 0]}}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "w_sum": {"$sum": "$rate_n", "window": window},  # nulls are ignored by $sum
                    "w_cnt": {"$sum": "$rate_ind", "window": window},
                },
            }
        },
        {
            "$addFields": {  # mean of non-null rates, 4 decimals half-up: floor((2*sum + n) / (2*n))
                "mov_n": {
                    "$cond": [
                        {"$gt": ["$w_cnt", 0]},
                        {
                            "$toInt": {
                                "$floor": {
                                    "$divide": [
                                        {"$add": [{"$multiply": [2, "$w_sum"]}, "$w_cnt"]},
                                        {"$multiply": [2, "$w_cnt"]},
                                    ]
                                }
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_half": 1,
                "late_count": 1,
                "rate_n": 1,
                "mov_n": 1,
            }
        },
    ]


def trend_params(from_: str, to: str) -> tuple[date, date, int]:
    d_from, d_to = parse_date(from_, "from"), parse_date(to, "to")
    if d_to < d_from:
        unprocessable("to", "to must not be before from")
    days = (d_to - d_from).days + 1
    if days > 92:
        unprocessable("to", "range must not exceed 92 days")
    return d_from, d_to, days


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query()):
    d_from, _, days = trend_params(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    rows = employees.aggregate(department_trend_pipeline(department, d_from, days), hint=IDX_EMP_DEPT)
    items = [
        {
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": r["present_half"] / 2,
            "late_count": r["late_count"],
            "attendance_rate": r["rate_n"] / 10000 if r.get("rate_n") is not None else None,
            "moving_avg_7d": r["mov_n"] / 10000 if r.get("mov_n") is not None else None,
        }
        for r in rows
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 8. Explain endpoint
# --------------------------------------------------------------------------- #
ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _require(**params):
    for name, value in params.items():
        if value is None:
            unprocessable(name, "required for this endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Each branch builds the query with the SAME builder functions the real endpoint uses.
    if endpoint == "attendance_list":
        q = attendance_filter(emp_code, date_from, date_to, status)
        collection = logs.name
        command = {
            "find": collection,
            "filter": q,
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            start, end = month_bounds(month)
            collection, pipeline, hint = logs.name, employee_monthly_pipeline(emp_code, start, end), IDX_LOG_UNIQUE
        elif endpoint == "department_summary":
            _require(month=month)
            start, end = month_bounds(month)
            collection, pipeline, hint = employees.name, department_summary_pipeline(start, end, department), IDX_EMP_DEPT
        elif endpoint == "late_leaderboard":
            _require(month=month)
            start, end = month_bounds(month)
            collection, pipeline, hint = logs.name, late_leaderboard_pipeline(start, end, limit, department), IDX_LOG_DATE
        else:  # department_trend
            _require(department=department, **{"from": from_, "to": to})
            d_from, _, days = trend_params(from_, to)
            collection, pipeline, hint = employees.name, department_trend_pipeline(department, d_from, days), IDX_EMP_DEPT
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint}
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS)),
    }
