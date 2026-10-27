import os

import pytest

URL = os.environ.get("TEST_DATABASE_URL")


def pytest_collection_modifyitems(config, items):
    """With REQUIRE_DB_TESTS=1, a missing database is an error, not a silent skip."""
    if os.environ.get("REQUIRE_DB_TESTS") == "1" and not URL:
        raise pytest.UsageError("REQUIRE_DB_TESTS=1 but TEST_DATABASE_URL is not set")


@pytest.fixture()
def conn():
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    psycopg = pytest.importorskip("psycopg")
    from neuroscope.db import init_db

    with psycopg.connect(URL) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.commit()
        init_db(c)
        yield c
