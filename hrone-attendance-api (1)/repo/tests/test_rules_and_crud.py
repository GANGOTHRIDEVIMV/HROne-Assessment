"""Runs without MongoDB: pure rule tests + CRUD flows against mongomock.
(The three aggregation endpoints need a real MongoDB 6+; see tests/check_analytics.py.)
    pip install pytest httpx mongomock && pytest tests/test_rules_and_crud.py
"""
from datetime import datetime, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient

from app import main

UTC = timezone.utc


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=main.IST).astimezone(UTC)


def ms(dt):
    return main.dt_to_ms(dt)


# ---------------- pure rule tests ----------------
def test_late_grace_boundaries():
    f = lambda h, m, s: main.compute_late_minutes(ist(2026, 7, 6, h, m, s), "2026-07-06", "09:30")
    assert f(9, 40, 0) == 0
    assert f(9, 40, 1) == 10
    assert f(10, 15, 59) == 45
    assert f(9, 0, 0) == 0


def test_overtime_threshold_and_overnight():
    f = lambda pout: main.compute_overtime(pout, "2026-07-06", "09:30", "18:30")
    assert f(ist(2026, 7, 6, 18, 59, 59)) == 0
    assert f(ist(2026, 7, 6, 19, 0, 0)) == 30
    assert main.compute_overtime(ist(2026, 7, 7, 6, 40), "2026-07-06", "22:00", "06:00") == 40


def test_work_hours_half_up():
    pin = ist(2026, 7, 6, 9, 0)
    assert main.compute_work_hours(pin, ist(2026, 7, 6, 18, 0)) == 9.0
    # 9h 0m 18s = 9.005 h -> half-up 9.01 (float round() would give 9.0)
    assert main.compute_work_hours(pin, ist(2026, 7, 6, 18, 0, 18)) == 9.01
    assert main.compute_work_hours(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 9.12


def test_attendance_date_overnight():
    # 22:00-06:00 shift: 03:00 IST belongs to previous day; 21:55 to same day
    assert main.attendance_date(ist(2026, 7, 7, 3, 0), "22:00", "06:00") == "2026-07-06"
    assert main.attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"
    assert main.attendance_date(ist(2026, 7, 7, 3, 0), "09:30", "18:30") == "2026-07-07"


def test_round_helpers():
    assert main.div_half_up(1, 8, 2) == 0.13  # 0.125
    assert main.div_half_up(5, 3200, 4) == 0.0016
    assert main.div_half_up(3, 32, 4) == 0.0938  # 0.09375 -> half-up


# ---------------- CRUD against mongomock ----------------
@pytest.fixture()
def api(monkeypatch):
    mdb = mongomock.MongoClient(tz_aware=True)["t"]
    monkeypatch.setattr(main, "employees", mdb["employees"])
    monkeypatch.setattr(main, "logs", mdb["attendance_logs"])
    mdb["employees"].create_index("emp_code", unique=True)
    mdb["attendance_logs"].create_index([("emp_code", 1), ("date", 1)], unique=True)
    return TestClient(main.app)


def mk(api, code="EMP0001", **kw):
    body = {"emp_code": code, "name": "A", "email": "a@x.io", "department": "Eng", "joined_on": "2026-01-05", **kw}
    return api.post("/employees", json=body)


def test_employee_create_validation_and_dupes(api):
    r = mk(api)
    assert r.status_code == 201 and "_id" not in r.json() and isinstance(r.json()["created_at"], int)
    assert mk(api).status_code == 409
    assert mk(api, "BAD1").status_code == 422
    assert mk(api, "EMP0002", shift_start="09:00", shift_end="09:00").status_code == 422
    assert mk(api, "EMP0003", joined_on="2026-02-30").status_code == 422


def test_employee_pagination(api):
    for i in range(1, 6):
        mk(api, f"EMP000{i}", department="Eng" if i < 4 else "Ops")
    r = api.get("/employees", params={"page": 1, "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0002"] and r["total"] == 5
    assert api.get("/employees", params={"department": "Ops"}).json()["total"] == 2
    assert api.get("/employees", params={"page": 0}).status_code == 422
    assert api.get("/employees", params={"page_size": 101}).status_code == 422


def test_punch_in_out_flow(api):
    mk(api)
    t_in = ms(ist(2026, 7, 6, 10, 5, 7)) + 900  # sub-second part must be truncated
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})
    assert r.status_code == 201
    j = r.json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 35 and j["punch_in"] == t_in - 900
    assert "id" not in j and j["history"] == [] and j["work_hours"] is None
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in}).status_code == 409
    assert api.post("/attendance/punch-in", json={"emp_code": "NOPE", "punched_at": t_in}).status_code == 404
    for bad in (1783312500, 1783312500000.0, "1783312500000", True):
        assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422

    t_out = ms(ist(2026, 7, 6, 19, 10))
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_in - 900}).status_code == 422
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out})
    assert r.status_code == 200
    j = r.json()
    assert j["overtime_minutes"] == 40 and j["half_day"] is False and j["work_hours"] == 9.08
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out}).status_code == 409


def test_punch_out_across_midnight_and_24h(api):
    mk(api, shift_start="22:00", shift_end="06:00")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026, 7, 6, 21, 55))})
    too_late = ms(ist(2026, 7, 7, 21, 56))
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": too_late}).status_code == 422
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026, 7, 7, 6, 40))})
    j = r.json()
    assert r.status_code == 200 and j["date"] == "2026-07-06" and j["overtime_minutes"] == 40 and j["work_hours"] == 8.75


def test_punch_out_without_punch_in_404(api):
    mk(api)
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026, 7, 6, 18, 0))}).status_code == 404


def test_half_day(api):
    mk(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "WFH", "punched_at": ms(ist(2026, 7, 7, 9, 30))})
    j = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026, 7, 7, 13, 0))}).json()
    assert j["work_hours"] == 3.5 and j["half_day"] is True


def test_list_filters_and_422(api):
    mk(api)
    mk(api, "EMP0002")
    for code in ("EMP0001", "EMP0002"):
        for d in (6, 7):
            api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(ist(2026, 7, d, 9, 30))})
    r = api.get("/attendance", params={"page_size": 3}).json()
    assert r["total"] == 4 and len(r["items"]) == 3
    assert [(i["date"], i["emp_code"]) for i in r["items"]][:2] == [("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002")]
    assert api.get("/attendance", params={"emp_code": "EMP0002", "date_from": "2026-07-07"}).json()["total"] == 1
    assert api.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-01"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "07/01/2026"}).status_code == 422
    assert api.get("/attendance", params={"status": "BOGUS"}).status_code == 422


def test_regularize(api):
    mk(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026, 7, 7, 10, 5))})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(ist(2026, 7, 7, 19, 0))})
    base = {"reason": "biometric glitch", "regularized_by": "hr.admin"}
    p = lambda body: api.patch("/attendance/EMP0001/2026-07-07", json={**base, **body})

    new_in = ms(ist(2026, 7, 7, 9, 28))
    r = p({"punch_in": new_in, "work_hours": 99})  # derived fields from client are ignored
    assert r.status_code == 200
    j = r.json()
    assert j["late_minutes"] == 0 and j["work_hours"] == 9.53 and len(j["history"]) == 1
    ch = j["history"][0]["changes"]
    assert set(ch) == {"punch_in", "late_minutes", "work_hours"} and ch["punch_in"]["to"] == new_in
    assert ch["late_minutes"] == {"from": 35, "to": 0}

    assert p({"punch_in": new_in}).status_code == 422                       # changes nothing
    assert p({"punch_in": ms(ist(2026, 7, 8, 9, 0))}).status_code == 422    # leaves the attendance date
    assert p({"punch_out": new_in}).status_code == 422                      # out not after in
    assert p({"status": "LEAVE", "punch_in": new_in}).status_code == 422    # LEAVE + punch time
    assert p({"punch_in": None}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-09", json={**base, "status": "WFH"}).status_code == 404
    assert api.patch("/attendance/NOPE/2026-07-07", json={**base, "status": "WFH"}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-07-07", json={"status": "WFH"}).status_code == 422  # reason required

    r = p({"status": "LEAVE"})
    j = r.json()
    assert r.status_code == 200 and j["punch_in"] is None and j["punch_out"] is None and j["work_hours"] is None
    assert len(j["history"]) == 2 and j["history"][1]["changes"]["punch_in"]["to"] is None
    # back to presence requires a punch_in
    assert p({"status": "PRESENT"}).status_code == 422
    r = p({"status": "PRESENT", "punch_in": new_in})
    assert r.status_code == 200 and len(r.json()["history"]) == 3


def test_legacy_record_without_history_and_half_day(api):
    mk(api)
    main.logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
                          "punch_in": ist(2026, 7, 14, 9, 32), "punch_out": ist(2026, 7, 14, 18, 31),
                          "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    j = api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert j["history"] == [] and j["half_day"] is False
    r = api.patch("/attendance/EMP0001/2026-07-14", json={"reason": "typo fix", "regularized_by": "hr", "status": "WFH"})
    assert r.status_code == 200 and len(r.json()["history"]) == 1
