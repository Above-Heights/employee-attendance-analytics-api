"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

The routes follow openapi.yaml; DATA_MODEL.md describes the stored documents.
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from math import floor
from typing import Annotated, Any, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# Configuration is required instead of silently pointing at a hard-coded database.
# load_dotenv keeps local setup convenient, while real environment variables win.
mongo_uri = os.getenv("MONGO_URI")
mongo_db = os.getenv("MONGO_DB")
if not mongo_uri or not mongo_db:
    raise RuntimeError("MONGO_URI and MONGO_DB must be set")

client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[mongo_db]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def validate_iso_date(value):
    """Reject datetime strings and non-padded dates at the HTTP boundary."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be a valid YYYY-MM-DD date") from exc


ApiDate = Annotated[Date, BeforeValidator(validate_iso_date)]


def ensure_indexes() -> None:
    """Create the guarantees and access paths used by the starter endpoints."""
    # These two unique indexes are correctness guarantees, not just optimizations:
    # MongoDB selects one winner when duplicate requests arrive concurrently.
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="employees_department_emp_code",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING), ("emp_code", ASCENDING)],
        name="employees_joined_on_department_emp_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        name="employees_department_joined_on_emp_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_code_date",
    )
    # These indexes match the filters and sort order used by the two list endpoints.
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_emp_code",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date_emp_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="attendance_emp_code_punch_in",
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Index creation is idempotent, so every clean deployment can prepare itself.
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
# Instants stay UTC internally. IST is used only to interpret attendance days and
# shift boundaries; this prevents the host machine's timezone changing behavior.
def truncate_to_seconds(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(microsecond=0)


def datetime_from_epoch_ms(value: int) -> datetime:
    return truncate_to_seconds(datetime.fromtimestamp(value / 1000, tz=UTC))


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = value - epoch
    return (
        elapsed.days * 86_400_000
        + elapsed.seconds * 1000
        + elapsed.microseconds // 1000
    )


def parse_shift_time(value: str) -> time:
    return time.fromisoformat(value)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    local = punch_in.astimezone(IST)
    start = parse_shift_time(shift_start)
    end = parse_shift_time(shift_end)
    attendance_date = local.date()
    # An early-morning punch for an overnight shift belongs to the previous day.
    if end <= start and local.time() < end:
        attendance_date -= timedelta(days=1)
    return attendance_date


def shift_boundary(attendance_date: Date, shift_time: str, next_day: bool = False) -> datetime:
    boundary = datetime.combine(attendance_date, parse_shift_time(shift_time), IST)
    return boundary + timedelta(days=1) if next_day else boundary


def compute_late_minutes(
    punch_in: datetime, shift_start: str, attendance_date: Optional[Date] = None
) -> int:
    day = attendance_date or punch_in.astimezone(IST).date()
    start = shift_boundary(day, shift_start)
    late_seconds = (punch_in.astimezone(IST) - start).total_seconds()
    # Compare seconds first: 10:00 is on time, while 10:01 is late by 10 minutes.
    return floor(late_seconds / 60) if late_seconds > 10 * 60 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    # Decimal half-up is required by R4; Python's round(float) is half-even.
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = (seconds / Decimal(3600)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return float(hours)


def compute_overtime(
    punch_out: datetime, shift_start: str, shift_end: str, attendance_date: Date
) -> int:
    overnight = parse_shift_time(shift_end) <= parse_shift_time(shift_start)
    end = shift_boundary(attendance_date, shift_end, next_day=overnight)
    overtime_seconds = (punch_out.astimezone(IST) - end).total_seconds()
    # R3 discards overtime shorter than 30 complete minutes.
    return floor(overtime_seconds / 60) if overtime_seconds >= 30 * 60 else 0


def validate_punch_interval(punch_in: datetime, punch_out: datetime) -> None:
    elapsed = punch_out - punch_in
    if elapsed.total_seconds() <= 0:
        raise HTTPException(422, [{"msg": "punch_out must be after punch_in"}])
    if elapsed > timedelta(hours=24):
        raise HTTPException(422, [{"msg": "punch_out must be within 24 hours"}])


def derived_attendance_fields(
    employee: dict,
    attendance_date: Date,
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
) -> dict:
    """Compute every derived value from one final, validated record state."""
    if status not in {"PRESENT", "WFH", "ON_DUTY"}:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    # Presence states are validated before this helper is called.
    assert punch_in is not None
    late_minutes = compute_late_minutes(
        punch_in, employee["shift_start"], attendance_date
    )
    if punch_out is None:
        return {
            "work_hours": None,
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "half_day": False,
        }

    validate_punch_interval(punch_in, punch_out)
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": compute_overtime(
            punch_out,
            employee["shift_start"],
            employee["shift_end"],
            attendance_date,
        ),
        "half_day": work_hours < 4.50,
    }


def month_bounds(month: str) -> tuple[Date, Date]:
    """Return the first day and exclusive end of a validated calendar month."""
    try:
        start = Date.fromisoformat(f"{month}-01")
        end = (
            Date(start.year + 1, 1, 1)
            if start.month == 12
            else Date(start.year, start.month + 1, 1)
        )
    except ValueError as exc:
        raise HTTPException(422, [{"msg": "month must be a valid YYYY-MM"}]) from exc
    return start, end


def date_from_string(expression) -> dict:
    return {
        "$dateFromString": {
            "dateString": expression,
            "format": "%Y-%m-%d",
            "timezone": "UTC",
        }
    }


def log_is_working_day(variable: str = "log") -> dict:
    # MongoDB day-of-week values 2 through 6 are Monday through Friday.
    return {
        "$in": [
            {"$dayOfWeek": date_from_string(f"$${variable}.date")},
            [2, 3, 4, 5, 6],
        ]
    }


def round_half_up_expression(expression, places: int) -> dict:
    """Build MongoDB decimal half-up rounding for non-negative report values."""
    factor = Decimal128(Decimal(10) ** places)
    return {
        "$toDouble": {
            "$divide": [
                {
                    "$floor": {
                        "$add": [
                            {"$multiply": [{"$toDecimal": expression}, factor]},
                            Decimal128("0.5"),
                        ]
                    }
                },
                factor,
            ]
        }
    }


def monthly_log_lookup(start: Date, end: Date) -> dict:
    """Join one employee to only that month's attendance documents."""
    return {
        "$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {
                    "$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$emp_code", "$$employee_code"]},
                                {"$gte": ["$date", start.isoformat()]},
                                {"$lt": ["$date", end.isoformat()]},
                            ]
                        }
                    }
                },
                {
                    "$project": {
                        "_id": 0,
                        "date": 1,
                        "status": 1,
                        "work_hours": 1,
                        "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                        "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                        "half_day": {"$ifNull": ["$half_day", False]},
                    }
                },
            ],
            "as": "logs",
        }
    }


def employee_log_metrics_stage() -> dict:
    """Calculate stored-value attendance metrics over an employee's `logs` array."""
    presence = {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]}
    return {
        "$set": {
            "present_days": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "log",
                        "in": {
                            "$cond": [
                                {"$and": [presence, log_is_working_day()]},
                                {"$cond": ["$$log.half_day", 0.5, 1]},
                                0,
                            ]
                        },
                    }
                }
            },
            "leave_days": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "log",
                        "in": {"$cond": [{"$eq": ["$$log.status", "LEAVE"]}, 1, 0]},
                    }
                }
            },
            "late_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "log",
                        "in": {"$cond": [{"$gt": ["$$log.late_minutes", 0]}, 1, 0]},
                    }
                }
            },
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "total_overtime_minutes": {"$sum": "$logs.overtime_minutes"},
            "on_duty_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "log",
                        "in": {"$cond": [{"$eq": ["$$log.status", "ON_DUTY"]}, 1, 0]},
                    }
                }
            },
            "work_hours_total": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "log",
                        "in": {
                            "$cond": [
                                {
                                    "$and": [
                                        presence,
                                        {"$ne": [{"$ifNull": ["$$log.work_hours", None]}, None]},
                                    ]
                                },
                                "$$log.work_hours",
                                0,
                            ]
                        },
                    }
                }
            },
            "work_hours_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "log",
                        "in": {
                            "$cond": [
                                {
                                    "$and": [
                                        presence,
                                        {"$ne": [{"$ifNull": ["$$log.work_hours", None]}, None]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        },
                    }
                }
            },
        }
    }


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    start, end = month_bounds(month)
    start_string = start.isoformat()
    end_string = end.isoformat()
    return [
        {"$match": {"emp_code": emp_code}},
        monthly_log_lookup(start, end),
        {
            "$set": {
                "effective_start": {
                    "$cond": [
                        {"$gt": ["$joined_on", start_string]},
                        "$joined_on",
                        start_string,
                    ]
                }
            }
        },
        {
            "$set": {
                "working_dates": {
                    "$cond": [
                        {"$lt": ["$effective_start", end_string]},
                        {
                            "$let": {
                                "vars": {
                                    "start_date": date_from_string("$effective_start"),
                                    "end_date": date_from_string(end_string),
                                },
                                "in": {
                                    "$map": {
                                        "input": {
                                            "$range": [
                                                0,
                                                {
                                                    "$dateDiff": {
                                                        "startDate": "$$start_date",
                                                        "endDate": "$$end_date",
                                                        "unit": "day",
                                                    }
                                                },
                                            ]
                                        },
                                        "as": "offset",
                                        "in": {
                                            "$dateAdd": {
                                                "startDate": "$$start_date",
                                                "unit": "day",
                                                "amount": "$$offset",
                                            }
                                        },
                                    }
                                },
                            }
                        },
                        [],
                    ]
                }
            }
        },
        {
            "$set": {
                "working_days": {
                    "$size": {
                        "$filter": {
                            "input": "$working_dates",
                            "as": "day",
                            "cond": {"$in": [{"$dayOfWeek": "$$day"}, [2, 3, 4, 5, 6]]},
                        }
                    }
                }
            }
        },
        employee_log_metrics_stage(),
        {
            "$set": {
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        round_half_up_expression(
                            {
                                "$multiply": [
                                    {"$divide": ["$present_days", "$working_days"]},
                                    100,
                                ]
                            },
                            4,
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": 1,
            }
        },
    ]


def build_department_summary_pipeline(
    month: str, department: Optional[str]
) -> list[dict]:
    start, end = month_bounds(month)
    employee_match = {"joined_on": {"$lt": end.isoformat()}}
    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        monthly_log_lookup(start, end),
        employee_log_metrics_stage(),
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_total": {"$sum": "$work_hours_total"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_days"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        round_half_up_expression(
                            {"$divide": ["$work_hours_total", "$work_hours_count"]},
                            2,
                        ),
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]


def build_late_leaderboard_pipeline(
    month: str, limit: int, department: Optional[str]
) -> list[dict]:
    """Rank monthly late totals in MongoDB, keeping all ties at the cutoff."""
    start, end = month_bounds(month)
    pipeline = [
        # The leading date filter uses the attendance date index before grouping.
        {"$match": {"date": {"$gte": start.isoformat(), "$lt": end.isoformat()}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        # Orphan logs have no employee and disappear here.
        {"$unwind": "$employee"},
    ]
    if department is not None:
        # Filter first: ranks are recalculated within the requested department.
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def build_department_trend_pipeline(
    department: str, date_from: Date, date_to: Date
) -> list[dict]:
    """Generate all requested days, join indexed counts, then average seven rows."""
    start = datetime.combine(date_from, time.min, UTC)
    day_count = (date_to - date_from).days + 1
    return [
        {"$match": {"department": department}},
        # Keep the department's codes and join dates together. This also anchors
        # the calendar when no employee has an attendance record in the range.
        {
            "$group": {
                "_id": None,
                "staff": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "staff": 1,
                "day": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": start,
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$day"},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}}},
        # A mid-range joiner enters the headcount and eligible log set on the
        # joining day. The filter runs over department employees only.
        {
            "$set": {
                "eligible_staff": {
                    "$filter": {
                        "input": "$staff",
                        "as": "person",
                        "cond": {"$lte": ["$$person.joined_on", "$date"]},
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date", "codes": "$eligible_staff.emp_code"},
                "pipeline": [
                    # Date equality uses the attendance date index; the code
                    # membership test excludes other departments and orphans.
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "attendance",
            }
        },
        {
            "$set": {
                "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
                "headcount": {"$size": "$eligible_staff"},
                "present_count": {
                    "$ifNull": [{"$arrayElemAt": ["$attendance.present_count", 0]}, 0]
                },
                "late_count": {
                    "$ifNull": [{"$arrayElemAt": ["$attendance.late_count", 0]}, 0]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        round_half_up_expression(
                            {
                                "$divide": [
                                    {"$toDecimal": "$present_count"},
                                    {"$toDecimal": "$headcount"},
                                ]
                            },
                            4,
                        ),
                        None,
                    ]
                }
            }
        },
        # The window is measured in calendar rows, so weekends occupy positions
        # but their null rates are excluded from the average by $avg.
        {
            "$setWindowFields": {
                "sortBy": {"day": ASCENDING},
                "output": {
                    "window_average": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$window_average", None]},
                        round_half_up_expression("$window_average", 4),
                        None,
                    ]
                },
            }
        },
    ]


def attendance_list_filter(
    emp_code: Optional[str],
    date_from: Optional[Date],
    date_to: Optional[Date],
    status: Optional["AttendanceStatus"],
) -> dict:
    """One filter builder keeps the list and its explain query in sync."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, [{"msg": "date_from must be on or before date_to"}])
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            query["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        query["status"] = status.value
    return query


def validate_trend_range(date_from: Date, date_to: Date) -> None:
    """Apply the same inclusive 92-day bound to trend and its explain mode."""
    span = (date_to - date_from).days + 1
    if span < 1 or span > 92:
        raise HTTPException(422, [{"msg": "date range must contain 1 to 92 days"}])


def api_value(value):
    """Recursively convert BSON datetimes, including values nested in history."""
    if isinstance(value, datetime):
        return datetime_to_epoch_ms(value)
    if isinstance(value, list):
        return [api_value(item) for item in value]
    if isinstance(value, dict):
        return {key: api_value(item) for key, item in value.items()}
    return value


def employee_response(doc: dict) -> dict:
    """Expose only contract fields and convert the stored BSON instant."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


def attendance_response(doc: dict) -> dict:
    """Normalize current and legacy records to the required response shape."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": api_value(doc.get("history", [])),
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class AttendanceStatus(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(json_schema_extra={"format": "date"})

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = Date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        if parsed.isoformat() != value:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        # Equal times are explicitly invalid; end < start represents overnight.
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


EpochMillis = Annotated[
    # strict=True rejects strings and floats instead of coercing them to integers.
    int, Field(strict=True, ge=100000000000, le=4102444800000)
]


class PunchInIn(BaseModel):
    emp_code: str
    # Omitted means use the server clock; an explicit JSON null is invalid.
    punched_at: EpochMillis = None  # type: ignore[assignment]
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = None  # type: ignore[assignment]


class RegularizeIn(BaseModel):
    # Defaults mean "omitted". Explicit JSON null still fails because these types
    # are non-nullable, matching the request schema.
    status: AttendanceStatus = None  # type: ignore[assignment]
    punch_in: EpochMillis = None  # type: ignore[assignment]
    punch_out: EpochMillis = None  # type: ignore[assignment]
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


class HealthOut(BaseModel):
    status: str


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str = Field(json_schema_extra={"format": "date"})
    created_at: EpochMillis


class EmployeePageOut(BaseModel):
    items: list[EmployeeOut]
    total: int
    page: int
    page_size: int


class AttendanceRecordOut(BaseModel):
    emp_code: str
    date: str = Field(json_schema_extra={"format": "date"})
    status: AttendanceStatus
    punch_in: Optional[EpochMillis]
    punch_out: Optional[EpochMillis]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[dict]


class AttendancePageOut(BaseModel):
    items: list[AttendanceRecordOut]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItemOut(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryOut(BaseModel):
    month: str
    items: list[DepartmentSummaryItemOut]


class LeaderboardItemOut(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardOut(BaseModel):
    month: str
    items: list[LeaderboardItemOut]


class TrendItemOut(BaseModel):
    date: str = Field(json_schema_extra={"format": "date"})
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class TrendOut(BaseModel):
    department: str
    items: list[TrendItemOut]


class ExplainEndpoint(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


class ExplainOut(BaseModel):
    endpoint: ExplainEndpoint
    collection: str
    explain: dict[str, Any]


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=HealthOut, operation_id="health")
def health():
    # Process health alone is insufficient: the API is ready only when MongoDB is.
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ok"}


@app.post(
    "/employees",
    status_code=201,
    response_model=EmployeeOut,
    operation_id="createEmployee",
)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(UTC))
    try:
        # Do not check before inserting. The unique index closes the race window.
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists")
    return employee_response(doc)


@app.get("/employees", response_model=EmployeePageOut, operation_id="listEmployees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    q = {}
    if department is not None:
        q["department"] = department
    # Count, sorting and pagination all use the same applied database filter.
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    docs = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [employee_response(doc) for doc in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post(
    "/attendance/punch-in",
    status_code=201,
    response_model=AttendanceRecordOut,
    operation_id="punchIn",
)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = (
        datetime_from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else truncate_to_seconds(datetime.now(UTC))
    )
    attendance_date = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    date_str = attendance_date.isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status.value,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], attendance_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # The unique (emp_code, date) index makes simultaneous punches atomic.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return attendance_response(doc)


@app.post(
    "/attendance/punch-out",
    response_model=AttendanceRecordOut,
    operation_id="punchOut",
)
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    punched_at = (
        datetime_from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else truncate_to_seconds(datetime.now(UTC))
    )
    # Select by the punch instant rather than calendar date so overnight shifts work.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")

    punch_in_at = record["punch_in"]
    validate_punch_interval(punch_in_at, punched_at)
    attendance_date = Date.fromisoformat(record["date"])
    derived = derived_attendance_fields(
        employee, attendance_date, record["status"], punch_in_at, punched_at
    )

    # Matching the original punch and null punch-out turns this into a compare-and-set.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_in": punch_in_at,
            "punch_out": None,
        },
        {"$set": {"punch_out": punched_at, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently")
    return attendance_response(updated)


@app.get(
    "/attendance",
    response_model=AttendancePageOut,
    operation_id="listAttendance",
)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Annotated[Optional[ApiDate], Query()] = None,
    date_to: Annotated[Optional[ApiDate], Query()] = None,
    status: Optional[AttendanceStatus] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    q = attendance_list_filter(emp_code, date_from, date_to, status)
    # Keep the full result set in MongoDB; Python receives only the requested page.
    total = db.attendance_logs.count_documents(q)
    docs = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [attendance_response(doc) for doc in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch(
    "/attendance/{emp_code}/{date}",
    response_model=AttendanceRecordOut,
    operation_id="regularizeAttendance",
)
def regularize_attendance(emp_code: str, date: ApiDate, body: RegularizeIn):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    date_str = date.isoformat()
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set
    final_status = (
        body.status.value if "status" in supplied else record["status"]
    )
    final_punch_in = (
        datetime_from_epoch_ms(body.punch_in)
        if "punch_in" in supplied
        else record.get("punch_in")
    )
    final_punch_out = (
        datetime_from_epoch_ms(body.punch_out)
        if "punch_out" in supplied
        else record.get("punch_out")
    )

    if final_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                422,
                [{"msg": "ABSENT and LEAVE corrections cannot include punch times"}],
            )
        # Switching to a non-presence status clears existing punch times.
        final_punch_in = None
        final_punch_out = None
    elif final_punch_in is None:
        raise HTTPException(422, [{"msg": "a presence status requires punch_in"}])

    if final_punch_in is not None:
        corrected_date = attendance_date_for(
            final_punch_in, employee["shift_start"], employee["shift_end"]
        )
        if corrected_date != date:
            raise HTTPException(
                422,
                [{"msg": "punch_in must remain on the record's attendance date"}],
            )
    if final_punch_out is not None:
        # The presence check above guarantees a punch-in here.
        assert final_punch_in is not None
        validate_punch_interval(final_punch_in, final_punch_out)

    derived = derived_attendance_fields(
        employee, date, final_status, final_punch_in, final_punch_out
    )
    final_values = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        **derived,
    }
    old_values = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    changes = {
        field: {"from": old_values[field], "to": final_values[field]}
        for field in final_values
        if old_values[field] != final_values[field]
    }
    if not changes:
        raise HTTPException(422, [{"msg": "the correction changes nothing"}])

    history_entry = {
        "at": truncate_to_seconds(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Match every mutable field as read. If another correction wins first, this
    # filter no longer matches, preventing a lost history entry or stale overwrite.
    snapshot_fields = [*old_values, "history"]
    snapshot_checks = [{"_id": record["_id"]}]
    for field in snapshot_fields:
        if field in record:
            snapshot_checks.append({field: record[field]})
        else:
            snapshot_checks.append({field: {"$exists": False}})

    updated = db.attendance_logs.find_one_and_update(
        {"$and": snapshot_checks},
        {"$set": final_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently")
    return attendance_response(updated)


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthlyOut,
    operation_id="employeeMonthly",
)
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
):
    # Starting from employees lets one pipeline distinguish an unknown employee
    # from a known employee who has no attendance records in the month.
    results = list(
        db.employees.aggregate(build_employee_monthly_pipeline(emp_code, month))
    )
    if not results:
        raise HTTPException(404, "employee not found")
    return results[0]


@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummaryOut,
    operation_id="departmentSummary",
)
def department_summary(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    department: Optional[str] = None,
):
    # Metrics and the cross-collection join are performed by MongoDB. Python only
    # wraps the aggregation rows in the contract's top-level response object.
    items = list(
        db.employees.aggregate(build_department_summary_pipeline(month, department))
    )
    return {"month": month, "items": items}


@app.get(
    "/analytics/leaderboard/late",
    response_model=LeaderboardOut,
    operation_id="lateLeaderboard",
)
def late_leaderboard(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    # Limit applies to rank after $rank, so tied employees can extend the list.
    items = list(
        db.attendance_logs.aggregate(
            build_late_leaderboard_pipeline(month, limit, department)
        )
    )
    return {"month": month, "items": items}


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=TrendOut,
    operation_id="departmentTrend",
)
def department_trend(
    department: str,
    date_from: Annotated[ApiDate, Query(alias="from")],
    date_to: Annotated[ApiDate, Query(alias="to")],
):
    validate_trend_range(date_from, date_to)
    # An empty pipeline result means no employee belongs to the department.
    items = list(
        db.employees.aggregate(
            build_department_trend_pipeline(department, date_from, date_to)
        )
    )
    if not items:
        raise HTTPException(404, "department not found")
    return {"department": department, "items": items}


@app.get(
    "/admin/explain/{endpoint}",
    response_model=ExplainOut,
    operation_id="explainEndpoint",
)
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = None,
    month: Annotated[
        Optional[str], Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    ] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[ApiDate] = None,
    date_to: Optional[ApiDate] = None,
    status: Optional[AttendanceStatus] = None,
    trend_from: Annotated[Optional[ApiDate], Query(alias="from")] = None,
    trend_to: Annotated[Optional[ApiDate], Query(alias="to")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """Run executionStats on the real endpoint's query shape."""
    if endpoint == ExplainEndpoint.attendance_list:
        collection = "attendance_logs"
        # A find command captures the exact list filter, sort, skip and limit.
        command = {
            "find": collection,
            "filter": attendance_list_filter(emp_code, date_from, date_to, status),
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint in {
            ExplainEndpoint.employee_monthly,
            ExplainEndpoint.department_summary,
            ExplainEndpoint.late_leaderboard,
        } and month is None:
            raise HTTPException(422, [{"msg": "month is required"}])

        if endpoint == ExplainEndpoint.employee_monthly:
            if emp_code is None:
                raise HTTPException(422, [{"msg": "emp_code is required"}])
            collection = "employees"
            pipeline = build_employee_monthly_pipeline(emp_code, month)
        elif endpoint == ExplainEndpoint.department_summary:
            collection = "employees"
            pipeline = build_department_summary_pipeline(month, department)
        elif endpoint == ExplainEndpoint.late_leaderboard:
            collection = "attendance_logs"
            pipeline = build_late_leaderboard_pipeline(month, limit, department)
        else:
            if department is None or trend_from is None or trend_to is None:
                raise HTTPException(
                    422, [{"msg": "department, from and to are required"}]
                )
            validate_trend_range(trend_from, trend_to)
            collection = "employees"
            pipeline = build_department_trend_pipeline(
                department, trend_from, trend_to
            )
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    # MongoDB returns BSON-specific values in explain. Relaxed Extended JSON
    # preserves its plan structure while making the response valid JSON.
    explained = db.command("explain", command, verbosity="executionStats")
    serializable = json.loads(json_util.dumps(explained))
    return {"endpoint": endpoint, "collection": collection, "explain": serializable}


