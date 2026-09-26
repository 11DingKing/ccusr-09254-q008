"""补录工作队列 API 测试：时钟推进、规则切换、租约回收与重启重建。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.workqueue import ManualClock
from app.db import get_db
from app.main import app
from app.routers import get_clock
from tests.conftest import TestSessionLocal

T0 = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture(autouse=True)
def _clock_override(clock: ManualClock):
    app.dependency_overrides[get_clock] = lambda: clock
    yield
    app.dependency_overrides.pop(get_clock, None)


def _create_rule(client: TestClient, **overrides) -> dict:
    body = {
        "rule_id": "makeup",
        "graduating_weight": 100.0,
        "materials_weight": 40.0,
        "aging_weight_per_hour": 5.0,
    }
    body.update(overrides)
    resp = client.post("/api/queue/rules", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _enqueue(
    client: TestClient,
    request_id: str,
    *,
    student: str = "S1",
    graduating: bool = False,
    materials: bool = False,
) -> dict:
    resp = client.post(
        "/api/queue/items",
        json={
            "request_id": request_id,
            "student_id": student,
            "graduating": graduating,
            "materials_complete": materials,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _claim(client: TestClient, worker: str = "W1", lease_seconds: int = 300):
    return client.post(
        "/api/queue/claim",
        json={"worker_id": worker, "lease_seconds": lease_seconds},
    )


def _preview_order(client: TestClient) -> list[str]:
    resp = client.get("/api/queue/items")
    assert resp.status_code == 200, resp.text
    return [item["request_id"] for item in resp.json()["items"]]


def test_first_rule_version_is_auto_activated(client):
    rule = _create_rule(client)
    assert rule["version"] == 1
    assert rule["active"] is True
    rules = client.get("/api/queue/rules").json()
    assert len(rules) == 1 and rules[0]["active"] is True


def test_enqueue_requires_active_rule(client):
    resp = client.post(
        "/api/queue/items", json={"request_id": "R-1", "student_id": "S1"}
    )
    assert resp.status_code == 409
    assert client.get("/api/queue/items").status_code == 409


def test_duplicate_enqueue_conflicts(client):
    _create_rule(client)
    _enqueue(client, "R-1")
    resp = client.post(
        "/api/queue/items", json={"request_id": "R-1", "student_id": "S2"}
    )
    assert resp.status_code == 409


def test_claim_empty_queue_returns_404(client):
    _create_rule(client)
    assert _claim(client).status_code == 404


def test_graduating_and_complete_materials_claimed_first(client):
    _create_rule(client)
    _enqueue(client, "PLAIN")
    _enqueue(client, "VIP", graduating=True, materials=True)
    claim = _claim(client)
    assert claim.status_code == 200
    assert claim.json()["item"]["request_id"] == "VIP"
    assert claim.json()["item"]["state"] == "leased"
    assert claim.json()["item"]["lease_owner"] == "W1"


def test_clock_advancement_aging_prevents_starvation(client, clock):
    _create_rule(client)
    _enqueue(client, "OLD")
    clock.advance(timedelta(hours=48))
    # 新到的高优先级请求：毕业 + 材料齐全 = 140 分
    _enqueue(client, "NEW", graduating=True, materials=True)
    items = client.get("/api/queue/items").json()["items"]
    # OLD 等待 48 小时，老化加分 48*5=240，超过 NEW 的 140 分
    assert [item["request_id"] for item in items] == ["OLD", "NEW"]
    assert items[0]["wait_seconds"] == pytest.approx(48 * 3600)
    assert items[0]["score"] == pytest.approx(240.0)
    assert items[1]["score"] == pytest.approx(140.0)


def test_rule_switch_changes_queue_order(client, clock):
    _create_rule(client, materials_weight=10.0, aging_weight_per_hour=0.0)
    _enqueue(client, "G", graduating=True)
    _enqueue(client, "M", materials=True)
    _enqueue(client, "O")
    assert _preview_order(client) == ["G", "M", "O"]

    # 新版本规则：不再照顾毕业生，材料齐全权重最高
    resp = client.post(
        "/api/queue/rules",
        json={
            "rule_id": "makeup",
            "graduating_weight": 0.0,
            "materials_weight": 50.0,
            "aging_weight_per_hour": 0.0,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["version"] == 2
    assert resp.json()["active"] is False
    # 未激活前顺序不变
    assert _preview_order(client) == ["G", "M", "O"]

    activated = client.post("/api/queue/rules/makeup/versions/2/activate")
    assert activated.status_code == 200
    assert activated.json()["active"] is True
    # 切换后 M(50) 居首，G 与 O 同分按请求标识排序
    assert _preview_order(client) == ["M", "G", "O"]
    stats = client.get("/api/queue/stats").json()
    assert stats["active_rule"]["version"] == 2


def test_activate_missing_rule_version_404(client):
    _create_rule(client)
    resp = client.post("/api/queue/rules/makeup/versions/99/activate")
    assert resp.status_code == 404


def test_return_for_supplement_keeps_original_wait(client, clock):
    _create_rule(client)
    enqueued = _enqueue(client, "R-1")
    clock.advance(timedelta(hours=5))
    token = _claim(client, lease_seconds=86400).json()["lease_token"]
    clock.advance(timedelta(hours=1))

    resp = client.post(
        "/api/queue/items/R-1/return",
        json={"lease_token": token, "reason": "缺少实习证明"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "pending"
    assert body["return_count"] == 1
    assert body["last_return_reason"] == "缺少实习证明"
    # 等待起点保持入队时刻，等待时间不退回零
    assert body["wait_anchor"] == enqueued["wait_anchor"]
    assert body["wait_seconds"] == pytest.approx(6 * 3600)
    # 退回后立即可被重新认领
    again = _claim(client, worker="W2")
    assert again.status_code == 200
    assert again.json()["item"]["request_id"] == "R-1"


def test_expired_lease_is_reclaimed_by_other_worker(client, clock):
    _create_rule(client)
    _enqueue(client, "R-1")
    first = _claim(client, worker="W1", lease_seconds=60)
    token1 = first.json()["lease_token"]

    clock.advance(timedelta(seconds=61))
    second = _claim(client, worker="W2", lease_seconds=60)
    assert second.status_code == 200
    assert second.json()["item"]["request_id"] == "R-1"
    assert second.json()["item"]["lease_owner"] == "W2"
    assert second.json()["lease_token"] != token1

    # 原持有者已失去租约：续租、退回、完成都被拒绝
    assert (
        client.post(
            "/api/queue/items/R-1/renew",
            json={"lease_token": token1, "lease_seconds": 60},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/queue/items/R-1/return",
            json={"lease_token": token1, "reason": "x"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/queue/items/R-1/complete", json={"lease_token": token1}
        ).status_code
        == 409
    )


def test_renew_extends_lease_and_blocks_reclaim(client, clock):
    _create_rule(client)
    _enqueue(client, "R-1")
    token = _claim(client, lease_seconds=60).json()["lease_token"]

    clock.advance(timedelta(seconds=50))
    renewed = client.post(
        "/api/queue/items/R-1/renew",
        json={"lease_token": token, "lease_seconds": 120},
    )
    assert renewed.status_code == 200
    expires = datetime.fromisoformat(renewed.json()["lease_expires_at"])
    assert expires == T0 + timedelta(seconds=170)

    # 原租约 60 秒已过，但续租后仍有效，他人无法认领
    clock.advance(timedelta(seconds=70))
    assert _claim(client, worker="W2").status_code == 404
    # 超过续租后的到期时刻才能被回收
    clock.advance(timedelta(seconds=51))
    reclaim = _claim(client, worker="W2")
    assert reclaim.status_code == 200
    assert reclaim.json()["item"]["lease_owner"] == "W2"


def test_lease_token_mismatch_rejected(client):
    _create_rule(client)
    _enqueue(client, "R-1")
    _claim(client)
    assert (
        client.post(
            "/api/queue/items/R-1/renew",
            json={"lease_token": "bogus", "lease_seconds": 60},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/queue/items/R-1/return",
            json={"lease_token": "bogus", "reason": "x"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/queue/items/R-1/complete", json={"lease_token": "bogus"}
        ).status_code
        == 409
    )


def test_operations_on_missing_item_404(client):
    _create_rule(client)
    assert (
        client.post(
            "/api/queue/items/NOPE/renew",
            json={"lease_token": "t", "lease_seconds": 60},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/queue/items/NOPE/return",
            json={"lease_token": "t", "reason": "x"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/queue/items/NOPE/complete", json={"lease_token": "t"}
        ).status_code
        == 404
    )


def test_full_flow_and_stats(client, clock):
    _create_rule(client)
    _enqueue(client, "R-1", graduating=True)
    _enqueue(client, "R-2")
    clock.advance(timedelta(hours=2))

    claim = _claim(client)
    assert claim.json()["item"]["request_id"] == "R-1"
    token = claim.json()["lease_token"]
    done = client.post(
        "/api/queue/items/R-1/complete", json={"lease_token": token}
    )
    assert done.status_code == 200
    assert done.json()["state"] == "completed"

    stats = client.get("/api/queue/stats").json()
    assert stats["completed"] == 1
    assert stats["pending"] == 1
    assert stats["claimable"] == 1
    assert stats["expired_leases"] == 0
    assert stats["oldest_wait_seconds"] == pytest.approx(2 * 3600)
    assert stats["average_wait_seconds"] == pytest.approx(2 * 3600)
    # 已完成条目不再出现在队列中
    assert _preview_order(client) == ["R-2"]


def test_stats_counts_expired_leases(client, clock):
    _create_rule(client)
    _enqueue(client, "R-1")
    _enqueue(client, "R-2")
    _claim(client, lease_seconds=30)
    clock.advance(timedelta(seconds=31))
    stats = client.get("/api/queue/stats").json()
    assert stats["leased"] == 1
    assert stats["expired_leases"] == 1
    assert stats["claimable"] == 2
    assert stats["pending"] == 1


def test_queue_rebuild_after_restart_keeps_same_order(client, clock):
    _create_rule(client)
    _enqueue(client, "R-1", graduating=True)
    clock.advance(timedelta(hours=2))
    _enqueue(client, "R-2", materials=True)
    clock.advance(timedelta(hours=2))
    _enqueue(client, "R-3")
    first_order = _preview_order(client)
    assert first_order == ["R-1", "R-2", "R-3"]

    # 模拟服务重启：全新的会话与客户端，同一个数据库文件
    session = TestSessionLocal()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as restarted:
            resp = restarted.get("/api/queue/items")
            assert resp.status_code == 200
            rebuilt_order = [item["request_id"] for item in resp.json()["items"]]
    finally:
        session.close()
    assert rebuilt_order == first_order
