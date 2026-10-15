from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


from app.main import app, db  # noqa: E402
from conftest import TEST_DB_NAME


def plan_stages(value):
    """Inspect nested plans, including $lookup subplans."""
    if isinstance(value, dict):
        if isinstance(value.get("stage"), str):
            yield value["stage"]
        for child in value.values():
            yield from plan_stages(child)
    elif isinstance(value, list):
        for child in value:
            yield from plan_stages(child)


def contains_key(value, key):
    """Execution stats may be nested under an aggregation's $cursor stage."""
    if isinstance(value, dict):
        return key in value or any(contains_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(contains_key(child, key) for child in value)
    return False


@pytest.fixture(scope="module")
def client():
    assert db.name == TEST_DB_NAME
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def sample_for_explain(client):
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.insert_one(
        {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
    )
    db.attendance_logs.insert_one(
        {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
    )
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


@pytest.mark.parametrize(
    ("endpoint", "params", "collection"),
    [
        ("attendance_list", {"date_from": "2026-07-01", "status": "PRESENT"}, "attendance_logs"),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}, "employees"),
        ("department_summary", {"month": "2026-07"}, "employees"),
        ("late_leaderboard", {"month": "2026-07"}, "attendance_logs"),
        (
            "department_trend",
            {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-08"},
            "employees",
        ),
    ],
)
def test_explain_uses_execution_stats_and_indexes(client, endpoint, params, collection):
    response = client.get(f"/admin/explain/{endpoint}", params=params)

    assert response.status_code == 200
    body = response.json()
    assert body["endpoint"] == endpoint
    assert body["collection"] == collection
    assert body["explain"]["command"]
    assert contains_key(body["explain"], "executionStats")
    stages = list(plan_stages(body["explain"]))
    assert any("IXSCAN" in stage for stage in stages)
    assert "COLLSCAN" not in stages


def test_attendance_explain_preserves_filter_sort_and_pagination(client):
    response = client.get(
        "/admin/explain/attendance_list",
        params={
            "emp_code": "EMP0001",
            "date_from": "2026-07-01",
            "date_to": "2026-07-31",
            "status": "PRESENT",
            "page": 3,
            "page_size": 4,
        },
    )

    assert response.status_code == 200
    command = response.json()["explain"]["command"]
    assert command["filter"] == {
        "emp_code": "EMP0001",
        "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"},
        "status": "PRESENT",
    }
    assert command["sort"] == {"date": -1, "emp_code": 1}
    assert command["skip"] == 8
    assert command["limit"] == 4


@pytest.mark.parametrize(
    ("endpoint", "params"),
    [
        ("employee_monthly", {"month": "2026-07"}),
        ("employee_monthly", {"emp_code": "EMP0001"}),
        ("department_summary", {}),
        ("late_leaderboard", {}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-06"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-08", "to": "2026-07-06"}),
        ("attendance_list", {"date_from": "2026-07-08", "date_to": "2026-07-06"}),
        ("attendance_list", {"page_size": 101}),
        ("late_leaderboard", {"month": "2026-07", "limit": 0}),
    ],
)
def test_explain_rejects_missing_or_invalid_parameters(client, endpoint, params):
    assert client.get(f"/admin/explain/{endpoint}", params=params).status_code == 422
