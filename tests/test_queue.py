"""补录公平队列服务层测试：评分、老化、规则版本、租约、并发认领与重启重建。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Barrier, Thread

import pytest

from app.models import QueueItem
from app.queue import service as queue_service
from app.queue.clock import clock
from app.queue.scoring import Rule
from tests.conftest import TestSessionLocal


@pytest.fixture(autouse=True)
def _frozen_clock():
    start = datetime(2026, 6, 1, 8, 0, 0, tzinfo=UTC)
    clock.freeze(start)
    yield clock
    clock.reset()


def _enqueue(student, *, graduating=False, materials=False):
    db = TestSessionLocal()
    try:
        return queue_service.enqueue(
            db,
            student_id=student,
            is_graduating=graduating,
            materials_ready=materials,
        )
    finally:
        db.close()


def _claim(worker="w1", lease_seconds=300):
    db = TestSessionLocal()
    try:
        return queue_service.claim(db, worker_id=worker, lease_seconds=lease_seconds)
    finally:
        db.close()


def _order():
    db = TestSessionLocal()
    try:
        return [item["id"] for item in queue_service.peek(db)]
    finally:
        db.close()


# ---------- 评分与排序 ----------

def test_priority_graduating_and_materials_first():
    normal = _enqueue("S-normal")
    clock.advance(timedelta(seconds=1))
    materials = _enqueue("S-materials", materials=True)
    clock.advance(timedelta(seconds=1))
    graduating = _enqueue("S-grad", graduating=True)
    clock.advance(timedelta(seconds=1))
    both = _enqueue("S-both", graduating=True, materials=True)

    assert _claim()["student_id"] == "S-both"
    assert _claim()["student_id"] == "S-grad"
    assert _claim()["student_id"] == "S-materials"
    assert _claim()["student_id"] == "S-normal"
    assert normal["id"] < materials["id"] < graduating["id"] < both["id"]


def test_ties_break_by_enqueue_time_then_id():
    _enqueue("A")
    clock.advance(timedelta(seconds=1))
    _enqueue("B")
    assert _claim()["student_id"] == "A"
    assert _claim()["student_id"] == "B"


def test_scoring_rule_pure_function():
    rule = Rule(rule_version=1, graduating_weight=100, materials_weight=50, aging_per_hour=10)
    now = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    since = now - timedelta(hours=3)
    assert rule.score(
        is_graduating=True, materials_ready=True, effective_since=since, now=now
    ) == pytest.approx(180.0)
    assert rule.score(
        is_graduating=False, materials_ready=False, effective_since=since, now=now
    ) == pytest.approx(30.0)


# ---------- 时钟推进 / 老化 ----------

def test_aging_lifts_old_normal_request_over_fresh_priority_one():
    # 老的普通请求 vs 刚入队、仅材料齐全的新请求：老化终将让普通请求排到前面，
    # 普通请求不会无限等待。
    _enqueue("S-old")
    clock.advance(timedelta(hours=6))
    _enqueue("S-new-materials", materials=True)  # 基础分 50
    # 默认老化 10/小时：老请求已等待 6 小时 -> 60 分 > 50
    assert _claim()["student_id"] == "S-old"


def test_clock_advance_reflects_in_peek_and_stats():
    item = _enqueue("S1")
    clock.advance(timedelta(hours=2, minutes=30))
    db = TestSessionLocal()
    try:
        out = queue_service.peek(db)[0]
        assert out["waited_hours"] == pytest.approx(2.5)
        assert out["score"] == pytest.approx(25.0)
        s = queue_service.stats(db)
        assert s["waiting"] == 1
        assert s["next_item_id"] == item["id"]
    finally:
        db.close()


# ---------- 规则版本化与切换 ----------

def test_rule_publish_keeps_history_and_new_rule_reorders():
    _enqueue("S-normal")
    clock.advance(timedelta(seconds=1))
    materials = _enqueue("S-materials", materials=True)

    db = TestSessionLocal()
    try:
        claimed = queue_service.claim(db, worker_id="w")
        assert claimed["student_id"] == "S-materials"
        queue_service.return_item(
            db, item_id=materials["id"], lease_token=claimed["lease_token"], reason="补证"
        )

        # 发布 v2：材料权重与老化归零 —— 两者同分，按入队时间，普通请求在前
        queue_service.publish_rule(
            db, graduating_weight=100, materials_weight=0, aging_per_hour=0
        )
        rules = queue_service.list_rules(db)
        assert [r["rule_version"] for r in rules] == [1, 2]
        assert rules[0]["is_active"] is False
        assert rules[1]["is_active"] is True

        assert queue_service.claim(db, worker_id="w")["student_id"] == "S-normal"
    finally:
        db.close()


# ---------- 租约：续租、退回、回收 ----------

def test_renew_and_return_preserves_wait_time():
    _enqueue("S1")
    claimed = _claim(lease_seconds=60)
    item_id = claimed["id"]
    token = claimed["lease_token"]
    original_effective = claimed["effective_since"]

    clock.advance(timedelta(seconds=30))
    db = TestSessionLocal()
    try:
        renewed = queue_service.renew(db, item_id=item_id, lease_token=token, lease_seconds=60)
        assert renewed["lease_expires_at"] == clock.now() + timedelta(seconds=60)
        with pytest.raises(queue_service.LeaseInvalidError):
            queue_service.renew(db, item_id=item_id, lease_token="deadbeef")
    finally:
        db.close()

    clock.advance(timedelta(seconds=30))
    db = TestSessionLocal()
    try:
        returned = queue_service.return_item(
            db, item_id=item_id, lease_token=token, reason="缺材料"
        )
        assert returned["status"] == "waiting"
        assert returned["effective_since"] == original_effective
        assert returned["last_return_reason"] == "缺材料"
    finally:
        db.close()

    # 等待时间自首次入队连续累计（已过 120s），不重新排队
    clock.advance(timedelta(seconds=60))
    again = _claim()
    assert again["id"] == item_id
    assert again["attempts"] == 2
    assert again["waited_hours"] == pytest.approx(120 / 3600, abs=1e-5)
    assert again["effective_since"] == original_effective


def test_expired_lease_is_reclaimable_and_active_lease_is_not():
    _enqueue("S1")
    claimed = _claim(worker="slow-worker", lease_seconds=60)
    token = claimed["lease_token"]

    # 租约有效期内：无条目可认领，续租成功
    clock.advance(timedelta(seconds=59))
    assert _claim() is None
    db = TestSessionLocal()
    try:
        s = queue_service.stats(db)
        assert s["leased"] == 1
        assert s["expired_leases"] == 0
        assert s["waiting"] == 0
        queue_service.renew(db, item_id=claimed["id"], lease_token=token, lease_seconds=60)
    finally:
        db.close()

    # 续租后再过 61 秒：租约过期，可被其他工作者回收
    clock.advance(timedelta(seconds=61))
    recovered = _claim(worker="recovery-worker")
    assert recovered is not None
    assert recovered["id"] == claimed["id"]
    assert recovered["lease_owner"] == "recovery-worker"
    assert recovered["attempts"] == 2
    assert recovered["lease_token"] != token

    # 旧 token 已失效：不能再续租或完成
    db = TestSessionLocal()
    try:
        with pytest.raises(queue_service.LeaseInvalidError):
            queue_service.renew(db, item_id=claimed["id"], lease_token=token, lease_seconds=60)
        with pytest.raises(queue_service.LeaseInvalidError):
            queue_service.complete_item(db, item_id=claimed["id"], lease_token=token)
    finally:
        db.close()


def test_complete_removes_from_claimable():
    _enqueue("S1")
    claimed = _claim()
    db = TestSessionLocal()
    try:
        done = queue_service.complete_item(
            db, item_id=claimed["id"], lease_token=claimed["lease_token"]
        )
        assert done["status"] == "completed"
        assert done["completed_at"] == clock.now()
        assert queue_service.claim(db, worker_id="w") is None
        s = queue_service.stats(db)
        assert s["completed"] == 1 and s["waiting"] == 0
    finally:
        db.close()


# ---------- 并发认领 ----------

def test_concurrent_claims_each_item_handled_once():
    n = 8
    for i in range(n):
        _enqueue(f"S{i}", graduating=(i % 3 == 0), materials=(i % 2 == 0))

    results: list[dict] = []
    barrier = Barrier(n)

    def worker(idx):
        barrier.wait()
        db = TestSessionLocal()
        try:
            item = queue_service.claim(db, worker_id=f"w{idx}", lease_seconds=300)
            results.append(item)
        finally:
            db.close()

    threads = [Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    claimed = [r for r in results if r is not None]
    ids = [r["id"] for r in claimed]
    assert len(claimed) == n
    assert len(ids) == len(set(ids))  # 没有任何条目被重复处理
    assert _claim() is None

    db = TestSessionLocal()
    try:
        rows = db.query(QueueItem).filter(QueueItem.status == "leased").all()
        assert len({(r.id, r.lease_token) for r in rows}) == n
    finally:
        db.close()


def test_concurrent_claims_contend_same_single_item():
    _enqueue("S-only")
    barrier = Barrier(5)
    winners: list = []

    def worker():
        barrier.wait()
        db = TestSessionLocal()
        try:
            item = queue_service.claim(db, worker_id="w", lease_seconds=300)
            if item is not None:
                winners.append(item["id"])
        finally:
            db.close()

    threads = [Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert winners == [1]


# ---------- 重启后重建相同顺序 ----------

def test_queue_rebuilds_same_order_from_persisted_state():
    # 混合等待中与租约过期条目，跨不同入队时间；顺序完全由持久化字段推导，
    # 进程重启后新会话重建出的顺序必须一致。
    _enqueue("A", materials=True)
    clock.advance(timedelta(hours=1))
    _enqueue("B")

    db = TestSessionLocal()
    try:
        claimed_a = queue_service.claim(db, worker_id="w1", lease_seconds=60)
    finally:
        db.close()
    assert claimed_a["student_id"] == "A"

    clock.advance(timedelta(hours=1))
    c = _enqueue("C", graduating=True)
    # A 的 60 秒租约早已过期（已过 1 小时）
    clock.advance(timedelta(hours=2))

    order_before = _order()

    # 用一批全新的会话（模拟重启：内存中没有任何队列状态）重建顺序
    def fresh_order():
        session = TestSessionLocal()
        try:
            return [item["id"] for item in queue_service.peek(session)]
        finally:
            session.close()

    order_after = fresh_order()
    assert order_after == order_before
    assert set(order_after) == {1, 2, 3}
    # C 是毕业学生（基础 100 + 3 小时老化 30），排第一；
    # A（50 + 4 小时老化 40）次之；B（0 + 2 小时老化 20）最后
    assert order_after == [c["id"], claimed_a["id"], 2]

    # 过期的 A 可在重建后被认领（租约回收不依赖内存状态）
    recovered = _claim()
    assert recovered["id"] == c["id"]
