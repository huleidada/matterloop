"""资源配额检查与原子记账测试。"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from matterloop_tools import QuotaExceededError, QuotaLimits, QuotaTracker


def test_quota_limits_validate_positive_values() -> None:
    with pytest.raises(ValueError):
        QuotaLimits(max_calls=0)
    with pytest.raises(ValueError):
        QuotaLimits(max_cpu_seconds=-1.0)


def test_tracker_consumes_within_limits() -> None:
    tracker = QuotaTracker()
    tracker.set_limits("tenant-a:agent-1", QuotaLimits(max_calls=2, max_tokens=100))

    tracker.check_and_consume("tenant-a:agent-1", tokens=40, cpu_seconds=1.5)
    tracker.check_and_consume("tenant-a:agent-1", tokens=60, gpu_seconds=0.5)

    usage = tracker.usage("tenant-a:agent-1")
    assert usage.calls == 2
    assert usage.tokens == 100
    assert usage.cpu_seconds == 1.5
    assert usage.gpu_seconds == 0.5


def test_tracker_rejects_over_limit_without_booking() -> None:
    tracker = QuotaTracker()
    tracker.set_limits("key", QuotaLimits(max_calls=10, max_tokens=5))

    with pytest.raises(QuotaExceededError):
        tracker.check_and_consume("key", tokens=6)

    # 超限调用的任何维度都不能落账，包括本可通过的 calls 维度。
    usage = tracker.usage("key")
    assert usage.calls == 0
    assert usage.tokens == 0


def test_tracker_books_tool_dimension_into_both_buckets() -> None:
    tracker = QuotaTracker()
    tracker.set_limits("key", QuotaLimits(max_calls=10))
    tracker.set_limits("key", QuotaLimits(max_calls=1), tool="shell")

    tracker.check_and_consume("key", tool="shell")

    assert tracker.usage("key").calls == 1
    assert tracker.usage("key", tool="shell").calls == 1

    with pytest.raises(QuotaExceededError) as excinfo:
        tracker.check_and_consume("key", tool="shell")

    assert excinfo.value.tool == "shell"
    assert tracker.usage("key").calls == 1
    assert tracker.usage("key", tool="shell").calls == 1

    # 主体桶未满，其他工具仍可继续调用。
    tracker.check_and_consume("key", tool="http")
    assert tracker.usage("key").calls == 2


def test_tracker_applies_default_limits_to_principal_bucket() -> None:
    tracker = QuotaTracker(default_limits=QuotaLimits(max_calls=1))

    tracker.check_and_consume("key")

    with pytest.raises(QuotaExceededError):
        tracker.check_and_consume("key")


def test_tracker_reset_clears_bookings() -> None:
    tracker = QuotaTracker()
    tracker.check_and_consume("key", tool="shell", tokens=10)

    tracker.reset("key", tool="shell")
    assert tracker.usage("key", tool="shell").tokens == 0
    assert tracker.usage("key").tokens == 10

    tracker.reset("key")
    assert tracker.usage("key").tokens == 0


def test_tracker_rejects_negative_consumption() -> None:
    tracker = QuotaTracker()

    with pytest.raises(ValueError):
        tracker.check_and_consume("key", calls=-1)
    with pytest.raises(ValueError):
        tracker.check_and_consume("key", cpu_seconds=-0.1)


def test_tracker_check_and_consume_is_atomic_under_threads() -> None:
    tracker = QuotaTracker()
    tracker.set_limits("key", QuotaLimits(max_calls=30))

    def attempt(_: int) -> bool:
        try:
            tracker.check_and_consume("key")
        except QuotaExceededError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        successes = sum(executor.map(attempt, range(100)))

    assert successes == 30
    assert tracker.usage("key").calls == 30
