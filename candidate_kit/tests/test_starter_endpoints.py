from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


from app.main import app, db  # noqa: E402
from conftest import TEST_DB_NAME


IST = timezone(timedelta(hours=5, minutes=30))


def epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def employee(emp_code: str, **overrides) -> dict:
    body = {
        "emp_code": emp_code,
        "name": f"Employee {emp_code}",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    body.update(overrides)
    return body


def create_and_punch_in(client, emp_code: str, punched_at: datetime, **employee_overrides):
    assert client.post(
        "/employees", json=employee(emp_code, **employee_overrides)
    ).status_code == 201
    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": emp_code, "punched_at": epoch_ms(punched_at)},
    )
    assert response.status_code == 201
    return response.json()


@pytest.fixture(scope="session")
def client():
    # The guard makes an accidental write to the sample/development database fail fast.
    assert db.name == TEST_DB_NAME
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_database(client):
    # Preserve indexes but isolate every test's documents from the next test.
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


def test_health_checks_database(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_employee_validates_and_serializes_created_at(client):
    response = client.post("/employees", json=employee("EMP0001"))

    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["created_at"], int)
    assert body["created_at"] % 1000 == 0
    assert "_id" not in body

    equal_shift = employee("EMP0002", shift_start="09:30", shift_end="09:30")
    assert client.post("/employees", json=equal_shift).status_code == 422
    assert client.post("/employees", json=employee("BAD1")).status_code == 422
    assert client.post(
        "/employees", json=employee("EMP0002", joined_on="2026-02-30")
    ).status_code == 422


def test_concurrent_employee_create_has_one_winner(client):
    payload = employee("EMP0100")

    # A real parallel request burst proves the database unique index settles the race.
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: client.post("/employees", json=payload).status_code, range(8)))

    assert statuses.count(201) == 1
    assert statuses.count(409) == 7
    assert db.employees.count_documents({"emp_code": "EMP0100"}) == 1


def test_employee_list_filters_sorts_and_paginates(client):
    client.post("/employees", json=employee("EMP0003", department="Sales"))
    client.post("/employees", json=employee("EMP0001"))
    client.post("/employees", json=employee("EMP0002"))

    page = client.get("/employees", params={"page": 1, "page_size": 2}).json()
    assert page["total"] == 3
    assert [item["emp_code"] for item in page["items"]] == ["EMP0001", "EMP0002"]

    engineering = client.get("/employees", params={"department": "Engineering"}).json()
    assert engineering["total"] == 2
    assert client.get("/employees", params={"page": 0}).status_code == 422
    assert client.get("/employees", params={"page_size": 101}).status_code == 422


@pytest.mark.parametrize("punched_at", [1_783_312_500, 1_783_312_500.0, "1783312500000"])
def test_punch_in_rejects_non_epoch_millisecond_integers(client, punched_at):
    client.post("/employees", json=employee("EMP0001"))
    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punched_at},
    )
    assert response.status_code == 422


def test_punch_in_unknown_employee_and_invalid_status(client):
    timestamp = epoch_ms(datetime(2026, 7, 6, 9, 30, tzinfo=IST))
    assert client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP9999", "punched_at": timestamp},
    ).status_code == 404

    client.post("/employees", json=employee("EMP0001"))
    assert client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": timestamp, "status": "ABSENT"},
    ).status_code == 422

    # The contract permits omitting punched_at, but not sending JSON null.
    assert client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": None},
    ).status_code == 422


def test_punch_in_grace_boundary_and_second_truncation(client):
    for code in ("EMP0001", "EMP0002", "EMP0003"):
        client.post("/employees", json=employee(code))

    # These three instants exercise both sides of R2's strict 10-minute boundary.
    cases = [
        ("EMP0001", datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST), 0),
        ("EMP0002", datetime(2026, 7, 6, 9, 40, 0, 900000, tzinfo=IST), 0),
        ("EMP0003", datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST), 10),
    ]
    for code, punched_at, expected_late in cases:
        response = client.post(
            "/attendance/punch-in",
            json={"emp_code": code, "punched_at": epoch_ms(punched_at)},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["late_minutes"] == expected_late
        assert body["punch_in"] % 1000 == 0
        assert "id" not in body


def test_overnight_punch_in_uses_previous_attendance_date(client):
    client.post(
        "/employees",
        json=employee("EMP0005", shift_start="22:00", shift_end="06:00"),
    )
    punched_at = epoch_ms(datetime(2026, 7, 7, 5, 59, tzinfo=IST))

    response = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0005", "punched_at": punched_at},
    )

    assert response.status_code == 201
    assert response.json()["date"] == "2026-07-06"


def test_concurrent_punch_in_has_one_winner(client):
    client.post("/employees", json=employee("EMP0001"))
    payload = {
        "emp_code": "EMP0001",
        "punched_at": epoch_ms(datetime(2026, 7, 6, 9, 30, tzinfo=IST)),
    }

    # All requests target one natural key; one must win and seven must conflict.
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(
            pool.map(lambda _: client.post("/attendance/punch-in", json=payload).status_code, range(8))
        )

    assert statuses.count(201) == 1
    assert statuses.count(409) == 7
    assert db.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1


def test_attendance_list_uses_required_order_filters_and_legacy_defaults(client):
    instant = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
    # Direct inserts include legacy shapes that intentionally omit history/half_day.
    db.attendance_logs.insert_many(
        [
            {
                "emp_code": "EMP0002",
                "date": "2026-07-07",
                "status": "PRESENT",
                "punch_in": instant,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
            },
            {
                "emp_code": "EMP0001",
                "date": "2026-07-07",
                "status": "WFH",
                "punch_in": instant,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
            },
            {
                "emp_code": "EMP0003",
                "date": "2026-07-06",
                "status": "LEAVE",
                "punch_in": None,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            },
        ]
    )

    response = client.get("/attendance", params={"page": 1, "page_size": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert [item["emp_code"] for item in body["items"]] == ["EMP0001", "EMP0002"]
    assert body["items"][0]["half_day"] is False
    assert body["items"][0]["history"] == []
    assert isinstance(body["items"][0]["punch_in"], int)
    assert "id" not in body["items"][0]

    leave = client.get("/attendance", params={"status": "LEAVE"}).json()
    assert leave["total"] == 1
    assert client.get(
        "/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-01"}
    ).status_code == 422


def test_punch_out_computes_work_overtime_and_half_day(client):
    punched_in = datetime(2026, 7, 6, 9, 30, tzinfo=IST)
    create_and_punch_in(client, "EMP0001", punched_in)

    response = client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms(datetime(2026, 7, 6, 18, 35, tzinfo=IST)),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["work_hours"] == 9.08
    assert body["overtime_minutes"] == 0
    assert body["half_day"] is False
    assert body["history"] == []

    # 1.005 hours must round half-up to 1.01, and therefore counts as half-day.
    create_and_punch_in(client, "EMP0002", punched_in)
    half_up = client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0002",
            "punched_at": epoch_ms(punched_in + timedelta(seconds=3618)),
        },
    ).json()
    assert half_up["work_hours"] == 1.01
    assert half_up["half_day"] is True


def test_overnight_punch_out_uses_next_day_shift_end(client):
    punched_in = datetime(2026, 7, 6, 21, 55, tzinfo=IST)
    create_and_punch_in(
        client,
        "EMP0005",
        punched_in,
        shift_start="22:00",
        shift_end="06:00",
    )

    response = client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0005",
            "punched_at": epoch_ms(datetime(2026, 7, 7, 6, 40, tzinfo=IST)),
        },
    )

    assert response.status_code == 200
    assert response.json()["date"] == "2026-07-06"
    assert response.json()["work_hours"] == 8.75
    assert response.json()["overtime_minutes"] == 40


def test_punch_out_errors_and_interval_limits(client):
    timestamp = datetime(2026, 7, 6, 9, 30, tzinfo=IST)
    assert client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP9999", "punched_at": epoch_ms(timestamp)},
    ).status_code == 404
    assert client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP9999", "punched_at": None},
    ).status_code == 422

    client.post("/employees", json=employee("EMP0001"))
    assert client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(timestamp)},
    ).status_code == 404

    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(timestamp)},
    )
    assert client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(timestamp)},
    ).status_code == 422
    assert client.post(
        "/attendance/punch-out",
        json={
            "emp_code": "EMP0001",
            "punched_at": epoch_ms(timestamp + timedelta(hours=24, seconds=1)),
        },
    ).status_code == 422

    valid = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(timestamp + timedelta(hours=8))},
    )
    assert valid.status_code == 200
    assert client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(timestamp + timedelta(hours=9))},
    ).status_code == 409


def test_concurrent_punch_out_has_one_winner(client):
    punched_in = datetime(2026, 7, 6, 9, 30, tzinfo=IST)
    create_and_punch_in(client, "EMP0001", punched_in)
    payload = {
        "emp_code": "EMP0001",
        "punched_at": epoch_ms(punched_in + timedelta(hours=9)),
    }

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(
            pool.map(
                lambda _: client.post("/attendance/punch-out", json=payload).status_code,
                range(8),
            )
        )

    assert statuses.count(200) == 1
    assert statuses.count(409) == 7


def test_regularization_recomputes_fields_and_records_exact_changes(client):
    original_in = datetime(2026, 7, 7, 10, 5, tzinfo=IST)
    original_out = datetime(2026, 7, 7, 18, 30, tzinfo=IST)
    corrected_in = datetime(2026, 7, 7, 9, 28, tzinfo=IST)
    create_and_punch_in(client, "EMP0001", original_in)
    client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(original_out)},
    )

    response = client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "punch_in": epoch_ms(corrected_in),
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["late_minutes"] == 0
    assert body["work_hours"] == 9.03
    assert len(body["history"]) == 1
    entry = body["history"][0]
    assert entry["by"] == "hr.admin"
    assert entry["reason"] == "biometric glitch"
    assert set(entry["changes"]) == {"punch_in", "work_hours", "late_minutes"}
    assert entry["changes"]["punch_in"] == {
        "from": epoch_ms(original_in),
        "to": epoch_ms(corrected_in),
    }

    # Nested history instants stay BSON datetimes in MongoDB, not API integers.
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert isinstance(stored["history"][0]["changes"]["punch_in"]["from"], datetime)


def test_regularization_to_absent_clears_times_and_derived_values(client):
    punched_in = datetime(2026, 7, 6, 10, 0, tzinfo=IST)
    create_and_punch_in(client, "EMP0001", punched_in)
    client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": epoch_ms(punched_in + timedelta(hours=3))},
    )

    response = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "ABSENT",
            "reason": "incorrect biometric record",
            "regularized_by": "hr.admin",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ABSENT"
    assert body["punch_in"] is None
    assert body["punch_out"] is None
    assert body["work_hours"] is None
    assert body["late_minutes"] == 0
    assert body["overtime_minutes"] == 0
    assert body["half_day"] is False
    assert {"status", "punch_in", "punch_out", "work_hours", "late_minutes", "half_day"}.issubset(
        body["history"][0]["changes"]
    )


def test_regularization_rejects_invalid_and_no_op_changes(client):
    punched_in = datetime(2026, 7, 6, 9, 30, tzinfo=IST)
    create_and_punch_in(client, "EMP0001", punched_in)
    base = {"reason": "manual correction", "regularized_by": "hr.admin"}

    no_op = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={**base, "status": "PRESENT"},
    )
    assert no_op.status_code == 422

    absent_with_time = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={**base, "status": "ABSENT", "punch_in": epoch_ms(punched_in)},
    )
    assert absent_with_time.status_code == 422

    # Omitted means unchanged; explicit null is not part of this request schema.
    assert client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={**base, "punch_in": None},
    ).status_code == 422
    assert client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={**base, "status": None},
    ).status_code == 422

    wrong_day = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={**base, "punch_in": epoch_ms(punched_in + timedelta(days=1))},
    )
    assert wrong_day.status_code == 422

    assert client.patch(
        "/attendance/EMP9999/2026-07-06", json=base
    ).status_code == 404
    assert client.patch(
        "/attendance/EMP0001/2026-07-07", json=base
    ).status_code == 404
    assert client.patch(
        "/attendance/EMP0001/2026-07-06T00:00:00", json=base
    ).status_code == 422


def test_calendar_query_rejects_datetime_instead_of_date(client):
    assert client.get(
        "/attendance", params={"date_from": "2026-07-06T00:00:00"}
    ).status_code == 422


def test_concurrent_regularizations_never_lose_history(client):
    punched_in = datetime(2026, 7, 6, 9, 30, tzinfo=IST)
    create_and_punch_in(client, "EMP0001", punched_in)
    payloads = [
        {"status": "WFH", "reason": "approved remote work", "regularized_by": "manager.one"},
        {"status": "ON_DUTY", "reason": "approved client visit", "regularized_by": "manager.two"},
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda payload: client.patch(
                    "/attendance/EMP0001/2026-07-06", json=payload
                ),
                payloads,
            )
        )

    statuses = [response.status_code for response in responses]
    assert all(status in {200, 409} for status in statuses)
    assert statuses.count(200) >= 1
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert len(stored["history"]) == statuses.count(200)
