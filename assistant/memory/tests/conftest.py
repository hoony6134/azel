"""Runs the app in-process against real Postgres, inside throwaway schemas that are dropped afterwards.

On ruchba: docker compose run --rm memory .venv/bin/pytest -q tests
"""
import importlib
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).parent


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    home = Path("/config") if Path("/config/db_url").exists() else Path.home() / ".config/biseo"
    url = (home / "db_url").read_text().strip()
    schema = f"test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(url, autocommit=True) as con:
        con.execute(f"create schema {schema}")
    tmp = tmp_path_factory.mktemp("home")
    (tmp / "token").write_text("t0k\n")
    (tmp / "state_token_limited").write_text("l1m\n")
    sep = "&" if "?" in url else "?"
    mp = pytest.MonkeyPatch()
    mp.setenv("BISEO_HOME", str(tmp))
    mp.setenv("BISEO_DB_URL", f"{url}{sep}options=-csearch_path%3D{schema},public")
    mp.setenv("BISEO_STATE_SCHEMA", f"{schema}_state")
    mp.setenv("BISEO_LIMITED_IPS", "203.0.113.9")  # a test client from this IP gets the limited routes only
    sys.path.insert(0, str(HERE.parent))
    import memory_service
    m = importlib.reload(memory_service)
    try:
        # a tailnet peer: with BISEO_LIMITED_IPS set, anything else is treated as limited
        with TestClient(m.app, client=("100.64.0.8", 50000), headers={"Authorization": "Bearer t0k"}) as c:
            yield c
    finally:
        mp.undo()
        with psycopg.connect(url, autocommit=True) as con:
            con.execute(f"drop schema {schema} cascade")
            con.execute(f"drop schema if exists {schema}_state cascade")
