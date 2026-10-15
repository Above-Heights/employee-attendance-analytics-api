from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


from app.main import app, db  # noqa: E402
from conftest import TEST_DB_NAME


def employee(emp_code: str, department: str, joined_on: str) -> dict:
    return {
        "emp_code": emp_code,
        "name": f"Employee {emp_code}",
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": joined_on,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def log(
    emp_code: str,
    date: str,
    status: str,
    *,
    work_hours=None,
    late_minutes=0,
    overtime_minutes=0,
    half_day=False,
) -> dict:
    return {
        "emp_code": emp_code,
        "date": date,
        "status": status,
        "punch_in": None,
        "punch_out": None,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
        "history": [],
    }


@pytest.fixture(scope="module")
def client():
    assert db.name == TEST_DB_NAME
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_database(client):
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


def test_employee_monthly_handles_joiner_weekends_half_days_and_stored_values(client):
    db.employees.insert_one(employee("EMP0001", "Engineering", "2026-07-13"))
    db.attendance_logs.insert_many(
        [
            log(
                "EMP0001",
                "2026-07-13",
                "PRESENT",
                work_hours=8.0,
                late_minutes=15,
                overtime_minutes=10,
            ),
            log(
                "EMP0001",
                "2026-07-14",
                "WFH",
                work_hours=3.5,
                half_day=True,
            ),
            # Weekend presence is excluded from present_days but still contributes
            # to late and overtime totals under R7.
            log(
                "EMP0001",
                "2026-07-18",
                "ON_DUTY",
                work_hours=5.0,
                late_minutes=30,
                overtime_minutes=40,
            ),
            log("EMP0001", "2026-07-15", "LEAVE"),
        ]
    )

    response = client.get(
        "/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "emp_code": "EMP0001",
        "month": "2026-07",
        "working_days": 15,
        "present_days": 1.5,
        "leave_days": 1,
        "late_count": 2,
        "total_late_minutes": 45,
        "total_overtime_minutes": 50,
        "attendance_pct": 10.0,
    }


def test_employee_monthly_known_without_logs_and_unknown_employee(client):
    db.employees.insert_one(employee("EMP0002", "Engineering", "2026-08-01"))

    known = client.get(
        "/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}
    )
    assert known.status_code == 200
    assert known.json()["working_days"] == 0
    assert known.json()["present_days"] == 0
    assert known.json()["attendance_pct"] is None

    unknown = client.get(
        "/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}
    )
    assert unknown.status_code == 404
    assert client.get(
        "/analytics/employees/EMP0002/monthly", params={"month": "2026-13"}
    ).status_code == 422
    assert client.get(
        "/analytics/employees/EMP0002/monthly", params={"month": "0000-01"}
    ).status_code == 422


def test_department_summary_counts_zero_log_staff_and_uses_record_average(client):
    db.employees.insert_many(
        [
            employee("EMP0001", "Engineering", "2026-01-01"),
            employee("EMP0002", "Engineering", "2026-07-31"),  # no logs, still headcount
            employee("EMP0003", "Engineering", "2026-08-01"),  # not joined yet
            employee("EMP0004", "Sales", "2026-02-01"),  # zero-log department
        ]
    )
    db.attendance_logs.insert_many(
        [
            log("EMP0001", "2026-07-06", "PRESENT", work_hours=8.005),
            log(
                "EMP0001",
                "2026-07-07",
                "WFH",
                work_hours=4.0,
                late_minutes=12,
                half_day=True,
            ),
            log(
                "EMP0001",
                "2026-07-11",
                "ON_DUTY",
                work_hours=10.0,
                late_minutes=30,
                overtime_minutes=60,
            ),
            log("EMP0001", "2026-07-08", "LEAVE"),
            # This employee is excluded by joined_on, and the orphan has no employee.
            log("EMP0003", "2026-07-06", "PRESENT", work_hours=9.0),
            log("EMP9999", "2026-07-06", "PRESENT", work_hours=9.0),
        ]
    )

    response = client.get(
        "/analytics/departments/summary", params={"month": "2026-07"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "month": "2026-07",
        "items": [
            {
                "department": "Engineering",
                "headcount": 2,
                "present_days": 1.5,
                "avg_work_hours": 7.34,
                "late_count": 2,
                "total_late_minutes": 42,
                "leave_count": 1,
                "on_duty_count": 1,
            },
            {
                "department": "Sales",
                "headcount": 1,
                "present_days": 0.0,
                "avg_work_hours": None,
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            },
        ],
    }


def test_department_summary_filter_and_empty_result(client):
    db.employees.insert_many(
        [
            employee("EMP0001", "Engineering", "2026-01-01"),
            employee("EMP0002", "Sales", "2026-01-01"),
            employee("EMP0003", "Future", "2026-08-01"),
        ]
    )

    filtered = client.get(
        "/analytics/departments/summary",
        params={"month": "2026-07", "department": "Sales"},
    )
    assert filtered.status_code == 200
    assert [item["department"] for item in filtered.json()["items"]] == ["Sales"]

    future = client.get(
        "/analytics/departments/summary",
        params={"month": "2026-07", "department": "Future"},
    )
    assert future.status_code == 200
    assert future.json()["items"] == []


def test_late_leaderboard_competition_ranks_cutoff_ties_and_department(client):
    db.employees.insert_many(
        [
            employee("EMP0001", "Engineering", "2026-01-01"),
            employee("EMP0002", "Engineering", "2026-01-01"),
            employee("EMP0003", "Sales", "2026-01-01"),
            employee("EMP0004", "Sales", "2026-01-01"),
            employee("EMP0005", "Sales", "2026-01-01"),
        ]
    )
    db.attendance_logs.insert_many(
        [
            log("EMP0001", "2026-07-06", "PRESENT", late_minutes=100),
            log("EMP0002", "2026-07-06", "PRESENT", late_minutes=50),
            log("EMP0003", "2026-07-06", "PRESENT", late_minutes=20),
            log("EMP0003", "2026-07-07", "WFH", late_minutes=30),
            log("EMP0004", "2026-07-06", "PRESENT", late_minutes=10),
            log("EMP0005", "2026-07-06", "PRESENT"),
            log("EMP9999", "2026-07-06", "PRESENT", late_minutes=200),
            log("EMP0004", "2026-08-01", "PRESENT", late_minutes=200),
        ]
    )

    # Rank 2 is tied: limit=2 returns three rows, and the next rank is 4.
    top = client.get(
        "/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}
    )
    assert top.status_code == 200
    assert [(row["rank"], row["emp_code"], row["total_late_minutes"])
            for row in top.json()["items"]] == [
        (1, "EMP0001", 100),
        (2, "EMP0002", 50),
        (2, "EMP0003", 50),
    ]
    assert top.json()["items"][2]["late_count"] == 2

    all_rows = client.get(
        "/analytics/leaderboard/late", params={"month": "2026-07", "limit": 4}
    ).json()["items"]
    assert [row["rank"] for row in all_rows] == [1, 2, 2, 4]

    sales = client.get(
        "/analytics/leaderboard/late",
        params={"month": "2026-07", "department": "Sales"},
    ).json()["items"]
    assert [(row["rank"], row["emp_code"]) for row in sales] == [
        (1, "EMP0003"), (2, "EMP0004")
    ]
    assert client.get(
        "/analytics/leaderboard/late", params={"month": "2026-07", "limit": 0}
    ).status_code == 422


def test_department_trend_fills_days_and_uses_daily_headcount_and_window(client):
    db.employees.insert_many(
        [
            employee("EMP0001", "Engineering", "2026-07-06"),
            employee("EMP0002", "Engineering", "2026-07-08"),
            employee("EMP0003", "Sales", "2026-01-01"),
        ]
    )
    db.attendance_logs.insert_many(
        [
            log("EMP0001", "2026-07-06", "PRESENT"),
            log("EMP0002", "2026-07-08", "WFH", half_day=True, late_minutes=12),
            log("EMP0001", "2026-07-11", "ON_DUTY", half_day=True, late_minutes=20),
            log("EMP0002", "2026-07-13", "PRESENT"),
            log("EMP0003", "2026-07-13", "PRESENT", late_minutes=40),
        ]
    )

    response = client.get(
        "/analytics/departments/Engineering/trend",
        params={"from": "2026-07-06", "to": "2026-07-13"},
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 8
    assert [row["date"] for row in items] == [f"2026-07-{day:02}" for day in range(6, 14)]
    assert [row["headcount"] for row in items] == [1, 1, 2, 2, 2, 2, 2, 2]
    assert [row["attendance_rate"] for row in items] == [1.0, 0.0, 0.25, 0.0, 0.0, None, None, 0.5]
    assert items[2]["present_count"] == 0.5
    assert items[2]["late_count"] == 1
    assert items[5]["is_working_day"] is False
    assert items[5]["present_count"] == 0.5
    assert items[5]["late_count"] == 1
    assert items[5]["moving_avg_7d"] == 0.25
    # On July 13 the 7-row window starts July 7; July 6 has left the window.
    assert items[7]["moving_avg_7d"] == 0.15


def test_department_trend_unknown_future_join_and_range_validation(client):
    db.employees.insert_one(employee("EMP0001", "Future", "2026-08-01"))
    params = {"from": "2026-07-06", "to": "2026-07-08"}
    future = client.get("/analytics/departments/Future/trend", params=params)
    assert future.status_code == 200
    assert all(row["headcount"] == 0 for row in future.json()["items"])
    assert all(row["attendance_rate"] is None for row in future.json()["items"])
    assert all(row["moving_avg_7d"] is None for row in future.json()["items"])

    assert client.get("/analytics/departments/Unknown/trend", params=params).status_code == 404
    assert client.get(
        "/analytics/departments/Future/trend",
        params={"from": "2026-07-08", "to": "2026-07-06"},
    ).status_code == 422
    assert client.get(
        "/analytics/departments/Future/trend",
        params={"from": "2026-07-01", "to": "2026-10-01"},
    ).status_code == 422
