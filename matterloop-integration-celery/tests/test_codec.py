"""Celery 请求 DTO 编解码测试。"""

from __future__ import annotations

import math

import pytest
from matterloop_core import LoopLimits, LoopRequest
from matterloop_integration_celery import CeleryMessageCodec, CeleryPayloadError


def test_codec_round_trips_request_as_json_data() -> None:
    codec = CeleryMessageCodec()
    request = LoopRequest(
        goal="完成任务",
        acceptance_criteria=("测试通过",),
        limits=LoopLimits(
            max_cycles=3,
            max_attempts=9,
            max_steps_per_plan=4,
            timeout_seconds=30.5,
        ),
        metadata={"trace": "t-1", "tags": ("safe",)},
    )

    payload = codec.encode_request(request)
    decoded = codec.decode_request(payload)

    assert payload["metadata"] == {"trace": "t-1", "tags": ["safe"]}
    assert decoded.goal == request.goal
    assert decoded.acceptance_criteria == request.acceptance_criteria
    assert decoded.limits == request.limits
    assert decoded.metadata == {"trace": "t-1", "tags": ["safe"]}


def test_codec_rejects_non_json_metadata() -> None:
    codec = CeleryMessageCodec()
    request = LoopRequest(goal="目标", metadata={"value": object()})

    with pytest.raises(CeleryPayloadError, match="JSON"):
        codec.encode_request(request)


def test_codec_rejects_non_finite_timeout() -> None:
    codec = CeleryMessageCodec()
    payload = codec.encode_request(LoopRequest(goal="目标"))
    limits = payload["limits"]
    assert isinstance(limits, dict)
    limits["timeout_seconds"] = math.inf

    with pytest.raises(CeleryPayloadError, match="timeout_seconds"):
        codec.decode_request(payload)
