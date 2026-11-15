"""测试装置：独立批次库、固定时区、样例加载。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

for entry in (str(ROOT), str(FIXTURES)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

os.environ["TZ"] = "UTC"
os.environ["GRIDLAB_DB"] = str(Path(tempfile.mkdtemp(prefix="gridlab-tests-")) / "case.db")

from app import config, store  # noqa: E402
from app.modules.m6_batches import service as batch_service  # noqa: E402


@pytest.fixture()
def conn():
    store.reset()
    connection = store.session()
    try:
        yield connection
    finally:
        connection.close()
        store.reset()


@pytest.fixture(autouse=True)
def _reset_publish_fault_switch():
    """每个测试前后都复位 M8 发布故障注入开关，避免全局状态跨用例泄漏。"""
    from app.modules.m8_publish import service as publish_service

    publish_service.FAIL_BEFORE_COMMIT = False
    yield
    publish_service.FAIL_BEFORE_COMMIT = False


@pytest.fixture()
def run_main(conn):
    return batch_service.execute_run(
        conn, sample="telemetry_raw", offsets="device_offsets"
    )


@pytest.fixture()
def run_gap(conn):
    return batch_service.execute_run(conn, sample="gap_series", offsets="device_offsets")


@pytest.fixture()
def run_edge(conn):
    return batch_service.execute_run(conn, sample="midnight_edge", offsets="device_offsets")


@pytest.fixture()
def api(conn):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture()
def sample_dir():
    return config.SAMPLE_DIR
