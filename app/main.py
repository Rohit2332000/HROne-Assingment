from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util
import json

# Existing environment variables take precedence over values in .env.
load_dotenv(override=False)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "employee_attendance")
IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]


def create_indexes() -> None:
    """Create the contract indexes idempotently at startup."""
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    # Email is not specified as unique by the contract; do not enforce extra business rules.
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined_on")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on_lookup")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_code_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_code")
    attendance.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="emp_code_date_desc")
    attendance.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp_code")
    attendance.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in_desc")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        client.admin.command("ping")
        create_indexes()
    except Exception:
        client.close()
        raise
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @field_validator("shift_end")
    @classmethod
    def shifts_must_differ(cls, value: str, info):
        if value == info.data.get("shift_start", "09:30"):
            raise ValueError("shift_start must differ from shift_end")
        return value


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def reject_explicit_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be omitted or an integer epoch-millisecond value")
        return self


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: StrictInt | None = None

    @model_validator(mode="after")
    def reject_explicit_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be omitted or an integer epoch-millisecond value")
        return self


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_explicit_null_fields(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} must be omitted or contain a valid value")
        return self


def error(code: int, message: str):
    raise HTTPException(status_code=code, detail=message)


def parse_day(value: str | date) -> date:
    if isinstance(value, datetime):
        error(422, "Date must use YYYY-MM-DD format.")
    if isinstance(value, date):
        return value
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        error(422, "Date must use YYYY-MM-DD format.")


def month_bounds(month: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        error(422, "month must use YYYY-MM format.")
    year, m = map(int, month.split("-"))
    first = date(year, m, 1)
    last = date(year + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    return first, last


def epoch_ms(value: int | None, default_now: bool = False) -> datetime | None:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0) if default_now else None
    # StrictInt rejects bool, float and string before this helper is called.
    if type(value) is not int or value < 100_000_000_000 or value > 4_102_444_800_000:
        error(422, "Timestamp must be integer epoch milliseconds in the supported range.")
    try:
        return datetime.fromtimestamp(value // 1000, UTC)
    except (ValueError, OverflowError, OSError):
        error(422, "Timestamp is out of range.")


def as_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def public_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert BSON datetimes to epoch ms and never expose Mongo's _id."""
    result = dict(doc)
    result.pop("_id", None)
    for field in ("created_at", "punch_in", "punch_out"):
        if field in result:
            result[field] = as_ms(result[field]) if isinstance(result[field], datetime) or result[field] is None else result[field]
    result.setdefault("history", [])
    for item in result["history"]:
        if isinstance(item.get("at"), datetime):
            item["at"] = as_ms(item["at"])
        for field in ("punch_in", "punch_out"):
            change = item.get("changes", {}).get(field)
            if change:
                for side in ("from", "to"):
                    if isinstance(change.get(side), datetime):
                        change[side] = as_ms(change[side])
    return result


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_at(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=IST)


def attendance_day(punch: datetime, employee: dict[str, Any]) -> date:
    local = punch.astimezone(IST)
    if employee["shift_end"] <= employee["shift_start"] and local.strftime("%H:%M") < employee["shift_end"]:
        return local.date() - timedelta(days=1)
    return local.date()


def shift_start(day: date, employee: dict[str, Any]) -> datetime:
    return shift_at(day, employee["shift_start"])


def shift_end(day: date, employee: dict[str, Any]) -> datetime:
    result = shift_at(day, employee["shift_end"])
    if employee["shift_end"] <= employee["shift_start"]:
        result += timedelta(days=1)
    return result


def late_minutes(punch_in: datetime | None, employee: dict[str, Any], day: date) -> int:
    if punch_in is None:
        return 0
    diff = (punch_in.astimezone(IST) - shift_start(day, employee)).total_seconds()
    return int(diff // 60) if diff > 600 else 0


def work_hours(punch_in: datetime | None, punch_out: datetime | None) -> float | None:
    if punch_in is None or punch_out is None:
        return None
    seconds = (punch_out - punch_in).total_seconds()
    if seconds < 0:
        error(422, "punch_out must be after punch_in.")
    return round_half_up(Decimal(str(seconds)) / Decimal(3600), 2)


def overtime_minutes(punch_out: datetime | None, employee: dict[str, Any], day: date) -> int:
    if punch_out is None:
        return 0
    diff = (punch_out.astimezone(IST) - shift_end(day, employee)).total_seconds()
    return int(diff // 60) if diff >= 1800 else 0


def calculated_fields(record: dict[str, Any], employee: dict[str, Any]) -> dict[str, Any]:
    day = parse_day(record["date"])
    pin, pout = record.get("punch_in"), record.get("punch_out")
    hours = work_hours(pin, pout)
    return {
        "late_minutes": late_minutes(pin, employee, day),
        "work_hours": hours,
        "overtime_minutes": overtime_minutes(pout, employee, day),
        "half_day": hours is not None and hours < 4.5,
    }


def get_employee(emp_code: str) -> dict[str, Any]:
    employee = employees.find_one({"emp_code": emp_code})
    if employee is None:
        error(404, "Employee not found.")
    return employee


def page_result(collection, query: dict[str, Any], sort, page: int, page_size: int):
    total = collection.count_documents(query)
    docs = list(collection.find(query, {"_id": 0}).sort(sort).skip((page - 1) * page_size).limit(page_size))
    return {"items": [public_doc(d) for d in docs], "total": total, "page": page, "page_size": page_size}


def workday_count(start: date, end: date) -> int:
    """Inclusive weekdays; used only for the scalar R7 denominator."""
    if start > end:
        return 0
    full_weeks, remainder = divmod((end - start).days + 1, 7)
    count = full_weeks * 5
    for offset in range(remainder):
        if (start + timedelta(days=full_weeks * 7 + offset)).weekday() < 5:
            count += 1
    return count


def month_log_pipeline(emp_code: str | None, start: date, end: date) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
    if emp_code:
        query["emp_code"] = emp_code
    return [
        {"$match": query},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                    {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},
                ]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        error(503, "MongoDB is unavailable.")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(payload: EmployeeCreate):
    data = payload.model_dump()
    data["joined_on"] = data["joined_on"].isoformat()
    data["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        employees.insert_one(data)
    except DuplicateKeyError:
        error(409, "Employee code already exists.")
    return public_doc(data)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    return page_result(employees, query, [("emp_code", ASCENDING)], page, page_size)


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(payload: PunchInRequest):
    employee = get_employee(payload.emp_code)
    instant = epoch_ms(payload.punched_at, default_now=True)
    day = attendance_day(instant, employee)
    doc = {
        "emp_code": payload.emp_code, "date": day.isoformat(), "status": payload.status,
        "punch_in": instant, "punch_out": None, "work_hours": None,
        "late_minutes": late_minutes(instant, employee, day), "overtime_minutes": 0,
        "half_day": False, "history": [],
    }
    try:
        attendance.insert_one(doc)  # unique (emp_code,date) index makes concurrent requests race-safe
    except DuplicateKeyError:
        error(409, "Attendance record already exists for this employee and date.")
    return public_doc(doc)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        error(422, "date_from must be on or before date_to.")
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()
    if status:
        query["status"] = status
    return page_result(attendance, query, [("date", DESCENDING), ("emp_code", ASCENDING)], page, page_size)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(payload: PunchOutRequest):
    employee = get_employee(payload.emp_code)
    instant = epoch_ms(payload.punched_at, default_now=True)

    # First find the latest open punch-in, even if it is
    # later than the requested punch-out timestamp.
    record = attendance.find_one(
        {
            "emp_code": payload.emp_code,
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        # Distinguish an already-closed record from no punch-in.
        existing = attendance.find_one(
            {"emp_code": payload.emp_code},
            sort=[("punch_in", DESCENDING)],
        )
        if existing and existing.get("punch_out") is not None:
            error(409, "This attendance record has already been punched out.")
        error(404, "No punch-in record found.")

    # Validate the timestamp before updating the database.
    if (
        instant <= record["punch_in"]
        or instant > record["punch_in"] + timedelta(hours=24)
    ):
        error(422, "Punch-out must be after punch-in and within 24 hours.")

    fields = calculated_fields(
        {**record, "punch_out": instant},
        employee,
    )

    result = attendance.find_one_and_update(
        {
            "emp_code": payload.emp_code,
            "date": record["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": instant,
                **fields,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if result is None:
        error(409, "Attendance record changed concurrently; retry.")

    return public_doc(result)


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize(emp_code: str, date: str, payload: RegularizeRequest):
    employee = get_employee(emp_code)
    day = parse_day(date)
    old = attendance.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if old is None:
        error(404, "Attendance record not found.")
    old.setdefault("history", [])
    status = payload.status if payload.status is not None else old["status"]
    if status in ("ABSENT", "LEAVE") and (payload.punch_in is not None or payload.punch_out is not None):
        error(422, "ABSENT and LEAVE cannot be supplied with punch times.")
    pin = epoch_ms(payload.punch_in) if payload.punch_in is not None else old.get("punch_in")
    pout = epoch_ms(payload.punch_out) if payload.punch_out is not None else old.get("punch_out")
    if status in PRESENCE_STATUSES and pin is None:
        error(422, "Presence statuses require punch_in.")
    if status in ("ABSENT", "LEAVE"):
        pin = pout = None
    if pin is not None and attendance_day(pin, employee) != day:
        error(422, "punch_in must belong to the attendance date.")
    if pout is not None and (pin is None or pout <= pin or pout > pin + timedelta(hours=24)):
        error(422, "punch_out must be after punch_in and within 24 hours.")
    candidate = {**old, "status": status, "punch_in": pin, "punch_out": pout}
    derived = calculated_fields(candidate, employee)
    proposed = {"status": status, "punch_in": pin, "punch_out": pout, **derived}
    changes: dict[str, Any] = {}
    for key, new_value in proposed.items():
        old_value = old.get(key)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        error(422, "The request does not change the record.")
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": payload.regularized_by,
             "reason": payload.reason, "changes": changes}
    # Compare-and-swap prevents concurrent regularizations from overwriting each other's changes/history.
    selector = {"emp_code": emp_code, "date": day.isoformat(), "status": old["status"],
                "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out")}
    if "history" in old:
        selector["history"] = old["history"]
    else:
        selector["history"] = {"$exists": False}
    result = attendance.find_one_and_update(selector,
        {"$set": proposed, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if result is None:
        error(409, "Attendance changed concurrently; reload and retry.")
    return public_doc(result)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str):
    employee = get_employee(emp_code)
    start, end = month_bounds(month)
    pipeline = month_log_pipeline(emp_code, start, end)
    result = next(iter(attendance.aggregate(pipeline)), {})
    joined = max(start, parse_day(employee["joined_on"]))
    working_days = workday_count(joined, end)
    present = round_half_up(result.get("present_days", 0), 2)
    attendance_pct = round_half_up(present / working_days * 100, 4) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days,
            "present_days": present, "leave_days": result.get("leave_days", 0),
            "late_count": result.get("late_count", 0),
            "total_late_minutes": result.get("total_late_minutes", 0),
            "total_overtime_minutes": result.get("total_overtime_minutes", 0),
            "attendance_pct": attendance_pct}


def build_department_summary_pipeline(start: date, end: date, department: str | None = None) -> list[dict[str, Any]]:
    match: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        match["department"] = department
    pipeline = [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}},
            {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1,
                          "late_minutes": 1, "overtime_minutes": 1}},
        ], "as": "logs"}},
        {"$set": {"stats": {"$reduce": {"input": "$logs", "initialValue": {
            "present_days": 0, "avg_hours_sum": 0, "avg_hours_count": 0, "late_count": 0,
            "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}, "in": {
                "present_days": {"$add": ["$$value.present_days", {"$cond": [
                    {"$and": [
                        {"$in": ["$$this.status", list(PRESENCE_STATUSES)]},
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$this.date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$this.date"}}}, 6]},
                    ]},
                    {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1]}, 0]}]},
                "avg_hours_sum": {"$add": ["$$value.avg_hours_sum", {"$cond": [
                    {"$and": [{"$in": ["$$this.status", list(PRESENCE_STATUSES)]}, {"$ne": ["$$this.work_hours", None]}]}, "$$this.work_hours", 0]}]},
                "avg_hours_count": {"$add": ["$$value.avg_hours_count", {"$cond": [
                    {"$and": [{"$in": ["$$this.status", list(PRESENCE_STATUSES)]}, {"$ne": ["$$this.work_hours", None]}]}, 1, 0]}]},
                "late_count": {"$add": ["$$value.late_count", {"$cond": [{"$gt": [ {"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0]}]},
                "total_late_minutes": {"$add": ["$$value.total_late_minutes", {"$ifNull": ["$$this.late_minutes", 0]}]},
                "leave_count": {"$add": ["$$value.leave_count", {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]}]},
                "on_duty_count": {"$add": ["$$value.on_duty_count", {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]}]},
            }}}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": "$stats.present_days"},
            "hours_sum": {"$sum": "$stats.avg_hours_sum"}, "hours_count": {"$sum": "$stats.avg_hours_count"},
            "late_count": {"$sum": "$stats.late_count"}, "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
            "leave_count": {"$sum": "$stats.leave_count"}, "on_duty_count": {"$sum": "$stats.on_duty_count"}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]}, {"$divide": ["$hours_sum", "$hours_count"]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(month: str, department: str | None = None):
    start, end = month_bounds(month)
    pipeline = build_department_summary_pipeline(start, end, department)
    items = list(employees.aggregate(pipeline))
    for item in items:
        item["present_days"] = round_half_up(item["present_days"], 2)
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


def build_leaderboard_pipeline(start: date, end: date, limit: int = 10, department: str | None = None) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                     "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    window: dict[str, Any] = {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}
    if department is not None:
        window["partitionBy"] = "$employee.department"
    pipeline += [
        {"$setWindowFields": window},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                       "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: str | None = None):
    start, end = month_bounds(month)
    pipeline = build_leaderboard_pipeline(start, end, limit, department)
    return {"month": month, "items": list(attendance.aggregate(pipeline))}


def build_trend_pipeline(department: str, from_day: date, to_day: date) -> list[dict[str, Any]]:
    start_dt = datetime.combine(from_day, time.min, tzinfo=UTC)
    end_dt = datetime.combine(to_day, time.min, tzinfo=UTC)
    pipeline = [
        {"$documents": [{"start": start_dt, "end": end_dt}]},
        {"$set": {"days": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": "$start", "endDate": "$end", "unit": "day"}}, 1]}]}}},
        {"$unwind": "$days"},
        {"$set": {"dayDate": {"$dateAdd": {"startDate": "$start", "unit": "day", "amount": "$days"}}}},
        {"$set": {"day": {"$dateToString": {"date": "$dayDate", "format": "%Y-%m-%d"}}}},
        {"$lookup": {"from": "employees", "let": {"day": "$day"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                   "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$dayDate"}, 2]}, {"$lte": [{"$dayOfWeek": "$dayDate"}, 6]}]} }},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$day"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
            {"$match": {"emp_code": {"$exists": True}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$unwind": "$e"}, {"$match": {"e.department": department}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [
                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}
            }], "as": "daily"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
                   "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$setWindowFields": {"sortBy": {"dayDate": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$day", "is_working_day": 1, "headcount": 1,
                       "present_count": 1, "late_count": 1,
                       "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(department: str, from_: date = Query(..., alias="from"), to: date = Query(...)):
    if to < from_ or (to - from_).days + 1 > 92:
        error(422, "Require from <= to and a date range of at most 92 days.")
    if not employees.find_one({"department": department}, {"_id": 1}):
        error(404, "Department not found.")
    # Generate calendar rows inside MongoDB (not in a Python loop), then indexed lookups fill the daily metrics.
    pipeline = build_trend_pipeline(department, from_, to)
    # $documents is a database-level stage, so execute using Database.aggregate.
    items = list(db.aggregate(pipeline))
    for item in items:
        item["present_count"] = round_half_up(item["present_count"], 2)
        if item["attendance_rate"] is not None:
            item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item["moving_avg_7d"] is not None:
            item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)
    return {"department": department, "items": items}


def _explain_find(query: dict[str, Any], sort: list[tuple[str, int]], page: int, page_size: int) -> dict[str, Any]:
    command = {"find": "attendance_logs", "filter": query, "sort": dict(sort),
               "skip": (page - 1) * page_size, "limit": page_size}
    return db.command("explain", command, verbosity="executionStats")


def _explain_aggregate(collection_name: str, pipeline: list[dict[str, Any]], allow_disk_use: bool = True) -> dict[str, Any]:
    command = {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}, "allowDiskUse": allow_disk_use}
    return db.command("explain", command, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: date | None = None,
    date_to: date | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_: date | None = Query(None, alias="from"),
    to: date | None = None,
):
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code is not None: query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from: query["date"]["$gte"] = date_from.isoformat()
            if date_to: query["date"]["$lte"] = date_to.isoformat()
        if status: query["status"] = status
        if date_from and date_to and date_from > date_to: error(422, "date_from must be on or before date_to.")
        explain = _explain_find(query, [("date", DESCENDING), ("emp_code", ASCENDING)], page, page_size)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(explain))}
    if endpoint == "employee_monthly":
        if not emp_code or not month: error(422, "emp_code and month are required for employee_monthly.")
        start, end = month_bounds(month)
        explain = _explain_aggregate("attendance_logs", month_log_pipeline(emp_code, start, end))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(explain))}
    if endpoint == "department_summary":
        if not month: error(422, "month is required for department_summary.")
        start, end = month_bounds(month)
        pipeline = build_department_summary_pipeline(start, end, department)
        explain = _explain_aggregate("employees", pipeline)
        return {"endpoint": endpoint, "collection": "employees", "explain": json.loads(json_util.dumps(explain))}
    if endpoint == "late_leaderboard":
        if not month: error(422, "month is required for late_leaderboard.")
        start, end = month_bounds(month)
        pipeline = build_leaderboard_pipeline(start, end, limit, department)
        explain = _explain_aggregate("attendance_logs", pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(explain))}
    if endpoint == "department_trend":
        if department is None or from_ is None or to is None: error(422, "department, from and to are required for department_trend.")
        if to < from_ or (to - from_).days + 1 > 92: error(422, "Require from <= to and a range of at most 92 days.")
        pipeline = build_trend_pipeline(department, from_, to)
        explain = db.command("explain", {"aggregate": 1, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(explain))}
    error(422, "Unsupported explain endpoint.")
