"""补录公平队列 HTTP 接口测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.queue.clock import clock


@pytest.fixture(autouse=True)
def _frozen_clock():
    clock.freeze(datetime(2026, 6, 1, 8, 0, 0, tzinfo=UTC))
    yield clock
    clock.reset()


def test_full_lifecycle_via_api(client):
    # 首次操作自动创建默认规则
    r = client.post(
        "/api/queue/items",
        json={"student_id": "S1", "is_graduating": True, "materials_ready": True},
    )
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["status"] == "waiting"
    assert item["score"] == pytest.approx(150.0)
    assert item["rule_version"] == 1

    # 认领
    r = client.post("/api/queue/claim", json={"worker_id": "staff-1", "lease_seconds": 120})
    assert r.status_code == 200, r.text
    claimed = r.json()
    assert claimed["id"] == item["id"]
    assert claimed["lease_owner"] == "staff-1"
    token = claimed["lease_token"]
    assert token

    # 队列已空 -> 204
    r = client.post("/api/queue/claim", json={"worker_id": "staff-2"})
    assert r.status_code == 204

    # 续租：错误 token 返回 409
    r = client.post(
        f"/api/queue/items/{item['id']}/renew",
        json={"lease_token": "wrong", "lease_seconds": 300},
    )
    assert r.status_code == 409

    r = client.post(
        f"/api/queue/items/{item['id']}/renew",
        json={"lease_token": token, "lease_seconds": 300},
    )
    assert r.status_code == 200
    assert r.json()["lease_expires_at"] is not None

    # 退回补证：等待时间保留
    r = client.post(
        f"/api/queue/items/{item['id']}/return",
        json={"lease_token": token, "reason": "缺少实习证明"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "waiting"
    assert body["effective_since"] == item["effective_since"]

    # 统计
    r = client.get("/api/queue/stats")
    assert r.status_code == 200
    stats = r.json()
    assert stats["waiting"] == 1
    assert stats["leased"] == 0
    assert stats["completed"] == 0
    assert stats["next_item_id"] == item["id"]
    assert stats["rule_version"] == 1


def test_rule_switch_via_api_reorders_queue(client):
    client.post("/api/queue/items", json={"student_id": "normal"})
    client.post("/api/queue/items", json={"student_id": "ready", "materials_ready": True})

    listing = client.get("/api/queue/items").json()
    assert [i["student_id"] for i in listing] == ["ready", "normal"]

    # 发布 v2：材料不再加分，只有毕业加分
    r = client.post(
        "/api/queue/rules",
        json={"graduating_weight": 100, "materials_weight": 0, "aging_per_hour": 0},
    )
    assert r.status_code == 201
    assert r.json()["rule_version"] == 2

    rules = client.get("/api/queue/rules").json()
    assert [rule["is_active"] for rule in rules] == [False, True]

    listing = client.get("/api/queue/items").json()
    # 同分按入队时间，normal 在前
    assert [i["student_id"] for i in listing] == ["normal", "ready"]

    stats = client.get("/api/queue/stats").json()
    assert stats["rule_version"] == 2


def test_lease_expiry_reclaim_via_api(client):
    item = client.post("/api/queue/items", json={"student_id": "S1"}).json()
    claimed = client.post(
        "/api/queue/claim", json={"worker_id": "w1", "lease_seconds": 60}
    ).json()
    token = claimed["lease_token"]

    # 未过期：不可被他人认领
    clock.advance(timedelta(seconds=59))
    assert client.post("/api/queue/claim", json={"worker_id": "w2"}).status_code == 204

    # 旧 token 在到期前可续租
    assert client.post(
        f"/api/queue/items/{item['id']}/renew",
        json={"lease_token": token, "lease_seconds": 60},
    ).status_code == 200

    # 过期后：w2 回收；统计中出现 expired_leases
    clock.advance(timedelta(seconds=61))
    stats = client.get("/api/queue/stats").json()
    assert stats["expired_leases"] == 1
    assert stats["next_item_id"] == item["id"]

    recovered = client.post(
        "/api/queue/claim", json={"worker_id": "w2", "lease_seconds": 60}
    ).json()
    assert recovered["id"] == item["id"]
    assert recovered["attempts"] == 2
    assert recovered["lease_token"] != token

    # w1 用旧 token 完成被拒
    r = client.post(
        f"/api/queue/items/{item['id']}/complete", json={"lease_token": token}
    )
    assert r.status_code == 409

    # w2 正常完成
    r = client.post(
        f"/api/queue/items/{item['id']}/complete",
        json={"lease_token": recovered["lease_token"]},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "completed"
    assert client.get("/api/queue/stats").json()["completed"] == 1


def test_operation_on_missing_item_returns_404(client):
    r = client.post(
        "/api/queue/items/999/renew", json={"lease_token": "x", "lease_seconds": 60}
    )
    assert r.status_code == 404


def test_invalid_rule_weights_rejected(client):
    r = client.post(
        "/api/queue/rules",
        json={"graduating_weight": -1, "materials_weight": 0, "aging_per_hour": 0},
    )
    assert r.status_code == 422
