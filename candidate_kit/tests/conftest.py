"""Choose a new disposable MongoDB database before test modules import the app."""

import os
from uuid import uuid4

import pytest


TEST_DB_NAME = f"attendance_test_{uuid4().hex}"
os.environ["MONGO_DB"] = TEST_DB_NAME


@pytest.fixture(scope="session", autouse=True)
def remove_disposable_test_database():
    yield
    from app.main import db

    # The exact generated name is the only database this test run may remove.
    assert db.name == TEST_DB_NAME
    db.client.drop_database(TEST_DB_NAME)
