"""Which database the tests run against.

In-memory SQLite by default (ADR-023) so the suite needs nothing installed.
Point `TEST_DATABASE_URL` at a Postgres server to run the same tests there:

    TEST_DATABASE_URL=postgresql://sentineliq:PASSWORD@localhost:5433/sentineliq_test

Use a database of its own — `sentineliq_test`, not `sentineliq`. Every test
starts by dropping the tables, so pointing this at the application's database
would delete the application's data.
"""

import os

import pytest

from sentineliq.components.database import repository as repo
from sentineliq.components.database.models import Base


def configured_url() -> str:
    """The database URL the tests should use."""
    return os.environ.get("TEST_DATABASE_URL", "sqlite://")


@pytest.fixture
def db_url():
    """The test database URL, with empty tables.

    A fresh in-memory SQLite is empty already. A real server keeps its rows
    between tests, so the tables are dropped and recreated first.
    """
    url = configured_url()
    if not url.startswith("sqlite"):
        engine = repo.build_engine(url)
        Base.metadata.drop_all(engine)
        repo.create_all(engine)
        engine.dispose()
    return url


@pytest.fixture
def engine(db_url):
    """An engine on the test database with the schema in place."""
    return repo.build_engine(db_url)
