"""并发认领测试：多个工作者同时认领时，每个条目只被认领一次。"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from app import queue_services
from tests.conftest import TestSessionLocal

T0 = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
ITEM_COUNT = 24
WORKER_COUNT = 8


def test_concurrent_claims_never_duplicate():
    with TestSessionLocal() as session:
        queue_services.create_rule(
            session,
            rule_id="makeup",
            graduating_weight=100.0,
            materials_weight=40.0,
            aging_weight_per_hour=5.0,
            aging_cap=None,
            now=T0,
        )
        for i in range(ITEM_COUNT):
            queue_services.enqueue(
                session,
                request_id=f"R-{i:02d}",
                student_id=f"S-{i:02d}",
                graduating=i % 3 == 0,
                materials_complete=i % 2 == 0,
                note="",
                now=T0,
            )

    claimed: list[dict] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        try:
            while True:
                with TestSessionLocal() as session:
                    try:
                        result = queue_services.claim(
                            session, worker_id=name, lease_seconds=300, now=T0
                        )
                    except queue_services.QueueEmptyError:
                        return
                with lock:
                    claimed.append(result)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(f"W-{k}",)) for k in range(WORKER_COUNT)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    request_ids = [c["item"]["request_id"] for c in claimed]
    assert len(request_ids) == ITEM_COUNT
    assert len(set(request_ids)) == ITEM_COUNT  # 没有重复认领
    tokens = [c["lease_token"] for c in claimed]
    assert len(set(tokens)) == ITEM_COUNT

    with TestSessionLocal() as session:
        summary = queue_services.stats(session, now=T0)
    assert summary["pending"] == 0
    assert summary["leased"] == ITEM_COUNT
    assert summary["claimable"] == 0
