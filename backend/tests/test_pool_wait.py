"""Queue wait on the shared mongo-io gather pool (P2.5): Database.gather measures how long each pooled task
waits before a pool thread picks it up, and a request reports the total in Server-Timing (dbq) and in its
log line (db_queue_ms). With the pool saturated (patched to one thread) the reported wait grows."""
import io
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import main
from app.core import database, observability
from app.core.database import Database

TASKS, TASK_SECONDS = 4, 0.05
ROUTE = "/api/test-pool-wait"


def _timing(res) -> dict[str, float]:
    return {m.group(1): float(m.group(2)) for m in re.finditer(r"(\w+);dur=([\d.]+)", res.headers["server-timing"])}


@pytest.fixture
def gather_route():
    """A route that gathers TASKS tasks of TASK_SECONDS each, so the wait depends only on the pool's size."""

    def pool_wait() -> dict:
        db = Database()
        return {"results": db.gather(*[lambda i=i: (time.sleep(TASK_SECONDS), i)[1] for i in range(TASKS)])}

    main.app.add_api_route(ROUTE, pool_wait, methods=["GET"])
    yield
    main.app.router.routes[:] = [r for r in main.app.router.routes if getattr(r, "path", None) != ROUTE]


@pytest.fixture
def one_thread_pool(monkeypatch):
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mongo-io")
    monkeypatch.setattr(database, "_pool", pool)
    yield pool
    pool.shutdown(wait=True)


@pytest.fixture
def request_log():
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(observability.JsonFormatter())
    logger = logging.getLogger("nearshop.request")
    logger.addHandler(handler)
    yield buffer
    logger.removeHandler(handler)


def test_a_real_page_reports_its_queue_wait(client, ids):
    res = client.get(f"/api/products/{ids['charger']}")  # its queries are fanned out through gather
    assert res.status_code == 200
    timing = _timing(res)
    assert {"app", "db", "dbq"} <= set(timing)
    assert 0 <= timing["dbq"] <= timing["db"] <= timing["app"]


def test_gather_reports_its_queue_wait_in_server_timing(client, gather_route, request_log):
    res = client.get(ROUTE)
    assert res.status_code == 200 and res.json() == {"results": list(range(TASKS))}
    timing = _timing(res)
    assert "dbq" in timing
    assert timing["dbq"] < TASK_SECONDS * 1000 / 2  # 24 threads: every task starts at once
    (line,) = request_log.getvalue().splitlines()
    assert json.loads(line)["db_queue_ms"] == timing["dbq"]


def test_the_reported_wait_grows_when_the_pool_is_saturated(client, gather_route, monkeypatch, request_log):
    free = _timing(client.get(ROUTE))["dbq"]
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mongo-io")
    monkeypatch.setattr(database, "_pool", pool)
    try:
        res = client.get(ROUTE)
    finally:
        pool.shutdown(wait=True)
    assert res.status_code == 200 and res.json() == {"results": list(range(TASKS))}
    timing = _timing(res)
    saturated = timing["dbq"]
    # one thread: the tasks wait 0, 1, 2 and 3 task-lengths, 6 in all (300 ms), summed over the tasks
    assert free < TASK_SECONDS * 1000 / 2
    assert saturated >= 0.8 * 6 * TASK_SECONDS * 1000 > free
    assert json.loads(request_log.getvalue().splitlines()[-1])["db_queue_ms"] == saturated
    assert timing["db"] >= 3 * TASK_SECONDS * 1000 * 0.8  # the waits count as database time, overlaps once


def test_each_pooled_task_reports_its_own_wait(one_thread_pool):
    waits: list[float] = []
    token = database.queue_wait_sink.set(lambda submitted, started: waits.append(started - submitted))
    try:
        results = Database().gather(*[lambda i=i: (time.sleep(TASK_SECONDS), i)[1] for i in range(3)])
    finally:
        database.queue_wait_sink.reset(token)
    assert results == [0, 1, 2]
    assert len(waits) == 3
    waits.sort()
    assert waits[0] < TASK_SECONDS / 2
    assert waits[1] >= 0.8 * TASK_SECONDS and waits[2] >= 0.8 * 2 * TASK_SECONDS


def test_inline_calls_and_no_listener_report_nothing():
    waits: list[float] = []
    token = database.queue_wait_sink.set(lambda submitted, started: waits.append(started - submitted))
    try:
        assert Database().gather(lambda: 1) == [1]  # a single query runs inline, never queued
    finally:
        database.queue_wait_sink.reset(token)
    assert waits == []
    assert Database().gather(lambda: 1, lambda: 2) == [1, 2]  # no listener: gather works as before
