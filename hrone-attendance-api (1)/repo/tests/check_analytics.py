"""Self-check for the analytics + explain endpoints. Needs a REAL MongoDB 6+.

    export MONGO_URI="mongodb://localhost:27017"      # or your Atlas URI
    export MONGO_DB="attendance_check"                # scratch DB - this script DROPS it
    python tests/check_analytics.py

It seeds awkward data (ties, weekend records, joiners mid-month, employees with no logs,
legacy records, unknown-employee logs), then compares every analytics endpoint with a plain
Python calculation of the rules, and checks that /admin/explain shows IXSCAN and no COLLSCAN.
"""
import json
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_DB", "attendance_check")
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

PRES = {"PRESENT", "WFH", "ON_DUTY"}
MONTH, START, END = "2026-07", date(2026, 7, 1), date(2026, 7, 31)
hu = lambda x, p: float(Decimal(str(x)).quantize(Decimal(1).scaleb(-p), ROUND_HALF_UP))


def seed():
    main.client.drop_database(main.db.name)
    rnd = random.Random(7)
    emps, logs = [], []
    depts = ["Eng", "Ops", "HR", "Empty"]
    for i in range(1, 41):
        dept = depts[i % 3] if i < 38 else "Empty"
        joined = "2026-01-05"
        if i % 7 == 0:
            joined = "2026-07-15"   # mid-month joiner
        if i == 39:
            joined = "2026-08-03"   # joins after the month
        emps.append({"emp_code": f"EMP{i:04d}", "name": f"N{i}", "email": "a@b.co", "department": dept,
                     "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined,
                     "created_at": datetime.now(timezone.utc)})
    for e in emps:
        i = int(e["emp_code"][3:])
        if i % 5 == 0:
            continue                # employees with no logs at all
        d = START
        while d <= END:
            if d.isoformat() >= e["joined_on"] and rnd.random() < (0.9 if d.weekday() < 5 else 0.15):
                st = rnd.choice(["PRESENT"] * 6 + ["WFH", "ON_DUTY", "LEAVE", "ABSENT"])
                doc = {"emp_code": e["emp_code"], "date": d.isoformat(), "status": st, "punch_in": None,
                       "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                       "half_day": False, "history": []}
                if st in PRES:
                    pin = datetime(d.year, d.month, d.day, 4, 0, tzinfo=timezone.utc)
                    doc["punch_in"] = pin
                    doc["late_minutes"] = rnd.choice([0, 0, 0, 11, 25, 25, 40])   # ties on purpose
                    if rnd.random() < 0.9:
                        wh = round(rnd.choice([3.5, 4.49, 4.5, 8.125, 8.5, 9.12, 9.005, 10.0]), 3)
                        wh = hu(wh, 2)
                        doc.update(punch_out=pin + timedelta(hours=wh), work_hours=wh, half_day=wh < 4.5,
                                   overtime_minutes=rnd.choice([0, 0, 35, 70]))
                if rnd.random() < 0.2:
                    doc.pop("half_day"), doc.pop("history")  # legacy shape
                logs.append(doc)
            d += timedelta(days=1)
    logs.append({"emp_code": "GHOST01", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 999,
                 "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)})  # unknown employee
    main.db.employees.insert_many(emps)
    main.db.attendance_logs.insert_many(logs)
    return emps, logs


def units(r):  # R5-R7: weekday presence only, half day = 0.5
    ok = r["status"] in PRES and date.fromisoformat(r["date"]).weekday() < 5
    return (0.5 if r.get("half_day") else 1.0) if ok else 0.0


def weekdays(a, b):
    n, d = 0, a
    while d <= b:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def check(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print("   got :", json.dumps(got)[:600])
        print("   want:", json.dumps(want)[:600])
    return ok


def main_check():
    emps, logs = seed()
    ok = True
    by_emp = {}
    for r in logs:
        by_emp.setdefault(r["emp_code"], []).append(r)
    edict = {e["emp_code"]: e for e in emps}
    inmonth = lambda r: START.isoformat() <= r["date"] <= END.isoformat()

    with TestClient(main.app) as c:
        # ---- employee monthly
        for code in ("EMP0001", "EMP0005", "EMP0007", "EMP0039"):
            e = edict[code]
            rs = [r for r in by_emp.get(code, []) if inmonth(r)]
            wd = weekdays(max(START, date.fromisoformat(e["joined_on"])), END) if e["joined_on"] <= END.isoformat() else 0
            pd = sum(units(r) for r in rs)
            want = {"emp_code": code, "month": MONTH, "working_days": wd, "present_days": pd,
                    "leave_days": sum(r["status"] == "LEAVE" for r in rs),
                    "late_count": sum(r["late_minutes"] > 0 for r in rs),
                    "total_late_minutes": sum(r["late_minutes"] for r in rs if r["late_minutes"] > 0),
                    "total_overtime_minutes": sum(r.get("overtime_minutes", 0) for r in rs),
                    "attendance_pct": hu(pd / wd * 100, 2) if wd else None}
            ok &= check(f"monthly {code}", c.get(f"/analytics/employees/{code}/monthly", params={"month": MONTH}).json(), want)
        ok &= check("monthly unknown 404", c.get("/analytics/employees/NOPE/monthly", params={"month": MONTH}).status_code, 404)

        # ---- department summary
        want_items = []
        for dept in sorted({e["department"] for e in emps}):
            es = [e for e in emps if e["department"] == dept and e["joined_on"] <= END.isoformat()]
            if not es:
                continue
            rs = [r for e in es for r in by_emp.get(e["emp_code"], []) if inmonth(r)]
            wh = [Decimal(str(r["work_hours"])) for r in rs if r["status"] in PRES and r.get("work_hours") is not None]
            want_items.append({"department": dept, "headcount": len(es), "present_days": sum(units(r) for r in rs),
                               "avg_work_hours": hu(sum(wh) / len(wh), 2) if wh else None,
                               "late_count": sum(r["late_minutes"] > 0 for r in rs),
                               "total_late_minutes": sum(r["late_minutes"] for r in rs if r["late_minutes"] > 0),
                               "leave_count": sum(r["status"] == "LEAVE" for r in rs),
                               "on_duty_count": sum(r["status"] == "ON_DUTY" for r in rs)})
        ok &= check("dept summary", c.get("/analytics/departments/summary", params={"month": MONTH}).json(),
                    {"month": MONTH, "items": want_items})
        ok &= check("dept summary filtered", c.get("/analytics/departments/summary", params={"month": MONTH, "department": "Eng"}).json()["items"],
                    [i for i in want_items if i["department"] == "Eng"])

        # ---- leaderboard (competition ranking, limit after ranking, tie order by emp_code)
        def board(limit, dept=None):
            tot = {}
            for code, rs in by_emp.items():
                if code not in edict or (dept and edict[code]["department"] != dept):
                    continue
                t = sum(r["late_minutes"] for r in rs if inmonth(r) and r["late_minutes"] > 0)
                if t > 0:
                    tot[code] = (t, sum(1 for r in rs if inmonth(r) and r["late_minutes"] > 0))
            rows = sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0]))
            out = []
            for idx, (code, (t, n)) in enumerate(rows):
                rank = 1 + sum(1 for _, (t2, _) in rows if t2 > t)
                out.append({"rank": rank, "emp_code": code, "name": edict[code]["name"], "department": edict[code]["department"],
                            "total_late_minutes": t, "late_count": n})
            return [r for r in out if r["rank"] <= limit]

        for lim, dept in ((5, None), (1, None), (10, "Ops"), (50, None)):
            params = {"month": MONTH, "limit": lim, **({"department": dept} if dept else {})}
            ok &= check(f"leaderboard limit={lim} dept={dept}", c.get("/analytics/leaderboard/late", params=params).json()["items"], board(lim, dept))

        # ---- trend
        for dept, a, b in (("Eng", date(2026, 7, 1), date(2026, 7, 31)), ("Ops", date(2026, 7, 10), date(2026, 7, 20)), ("Empty", date(2026, 7, 1), date(2026, 7, 9))):
            es = [e for e in emps if e["department"] == dept]
            rows, rates, d = [], [], a
            while d <= b:
                ds = d.isoformat()
                hc = sum(e["joined_on"] <= ds for e in es)
                rs = [r for e in es for r in by_emp.get(e["emp_code"], []) if r["date"] == ds]
                pc = sum((0.5 if r.get("half_day") else 1.0) for r in rs if r["status"] in PRES)
                wd = d.weekday() < 5
                rate = hu(pc / hc, 4) if wd and hc > 0 else None
                rates.append(rate)
                win = [x for x in rates[-7:] if x is not None]
                rows.append({"date": ds, "is_working_day": wd, "headcount": hc, "present_count": pc,
                             "late_count": sum(r["late_minutes"] > 0 for r in rs), "attendance_rate": rate,
                             "moving_avg_7d": hu(sum(Decimal(str(x)) for x in win) / len(win), 4) if win else None})
                d += timedelta(days=1)
            got = c.get(f"/analytics/departments/{dept}/trend", params={"from": a.isoformat(), "to": b.isoformat()}).json()
            ok &= check(f"trend {dept} {a}..{b}", got, {"department": dept, "items": rows})
        ok &= check("trend unknown dept 404", c.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-05"}).status_code, 404)
        ok &= check("trend >92 days 422", c.get("/analytics/departments/Eng/trend", params={"from": "2026-01-01", "to": "2026-05-01"}).status_code, 422)
        ok &= check("trend to<from 422", c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-05", "to": "2026-07-01"}).status_code, 422)

        # ---- explain: IXSCAN present, COLLSCAN absent
        calls = {
            "attendance_list": {"emp_code": "EMP0001", "date_from": "2026-07-01"},
            "employee_monthly": {"emp_code": "EMP0001", "month": MONTH},
            "department_summary": {"month": MONTH},
            "late_leaderboard": {"month": MONTH, "limit": 5},
            "department_trend": {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"},
        }
        for ep, params in calls.items():
            r = c.get(f"/admin/explain/{ep}", params=params)
            txt = json.dumps(r.json())
            ok &= check(f"explain {ep}: 200 + IXSCAN + no COLLSCAN", (r.status_code, "IXSCAN" in txt, "COLLSCAN" in txt), (200, True, False))
        ok &= check("explain missing param 422", c.get("/admin/explain/employee_monthly", params={"month": MONTH}).status_code, 422)
        ok &= check("explain bad endpoint 422", c.get("/admin/explain/nope").status_code, 422)

    main.client.drop_database(main.db.name)
    print("\nALL CHECKS PASSED" if ok else "\nSOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main_check()
