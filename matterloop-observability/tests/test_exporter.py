"""JSONL 与 OpenTelemetry 导出器测试。"""

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from matterloop_observability import JsonlExporter, OtelExporter, Score, SpanRecord


def _span(
    name: str = "root",
    *,
    trace_id: str = "run-1",
    span_id: str | None = None,
    parent_span_id: str | None = None,
) -> SpanRecord:
    """创建一条最小的跨度记录。"""
    moment = datetime.now(timezone.utc)
    return SpanRecord(
        trace_id=trace_id,
        span_id=span_id or uuid4().hex,
        parent_span_id=parent_span_id,
        name=name,
        observation_type="span",
        started_at=moment,
        ended_at=moment,
        attributes={"matterloop.run_id": trace_id},
    )


def _score() -> Score:
    """创建一条最小的评分记录。"""
    return Score(
        name="verification",
        value=0.8,
        data_type="NUMERIC",
        source="VERIFIER",
        run_id="run-1",
        step_id="step-1",
        comment="符合验收",
        evidence=("证据",),
    )


def test_jsonl_exporter_writes_typed_lines(tmp_path: Path) -> None:
    """JSONL 每行应带类型字段并保留跨度的树形信息。"""
    path = tmp_path / "nested" / "traces.jsonl"
    exporter = JsonlExporter(path)
    root = _span("root", span_id="a" * 32)
    child = _span("child", parent_span_id=root.span_id)

    exporter.export([root, child, _score()])

    payloads = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [payload["type"] for payload in payloads] == ["span", "span", "score"]
    assert payloads[0]["parent_span_id"] is None
    assert payloads[1]["parent_span_id"] == "a" * 32
    assert payloads[0]["attributes"] == {"matterloop.run_id": "run-1"}
    assert payloads[0]["started_at"]
    score_payload = payloads[2]
    assert score_payload["name"] == "verification"
    assert score_payload["value"] == 0.8
    assert score_payload["step_id"] == "step-1"
    assert score_payload["evidence"] == ["证据"]


def test_jsonl_exporter_appends_across_calls(tmp_path: Path) -> None:
    """多次导出应向同一文件追加而不是覆盖。"""
    path = tmp_path / "traces.jsonl"
    exporter = JsonlExporter(path)

    exporter.export([_span("first")])
    exporter.export([_span("second")])

    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_otel_exporter_requires_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """未安装 OpenTelemetry SDK 时构造应提示安装 otel extra。"""
    real_import = importlib.import_module

    def blocked(name: str, package: str | None = None) -> object:
        if name.startswith("opentelemetry"):
            raise ImportError(name)
        return real_import(name, package)

    monkeypatch.setattr("matterloop_observability.exporter.importlib.import_module", blocked)

    with pytest.raises(ImportError, match=r"matterloop-observability\[otel\]"):
        OtelExporter()


def test_otel_exporter_rejects_invalid_pending_capacity() -> None:
    """根跨度暂存容量必须为正数。"""
    with pytest.raises(ValueError, match="max_pending_items"):
        OtelExporter(tracer_provider=object(), max_pending_items=0)


def test_otel_exporter_bounds_records_waiting_for_root(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """根跨度迟到时的暂存应有界，且根本身不能被容量限制丢弃。"""
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    sdk_export = pytest.importorskip("opentelemetry.sdk.trace.export")
    in_memory = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")

    provider = sdk_trace.TracerProvider()
    memory = in_memory.InMemorySpanExporter()
    provider.add_span_processor(sdk_export.SimpleSpanProcessor(memory))
    exporter = OtelExporter(tracer_provider=provider, max_pending_items=1)
    root = _span("root", trace_id=uuid4().hex, span_id=uuid4().hex)
    child = _span("child", trace_id=root.trace_id, parent_span_id=root.span_id)
    score = Score(
        name="verification",
        value=0.8,
        data_type="NUMERIC",
        source="VERIFIER",
        run_id=root.trace_id,
    )

    exporter.export([child, score, root])

    assert "根跨度尚未到达" in caplog.text
    assert {span.name for span in memory.get_finished_spans()} == {"root", "child"}


def test_otel_exporter_rebuilds_tree_without_writing_sdk_private_state() -> None:
    """乱序批次也应通过公开 OTel API 还原树形关系。"""
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    sdk_export = pytest.importorskip("opentelemetry.sdk.trace.export")
    in_memory = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")

    provider = sdk_trace.TracerProvider()
    memory = in_memory.InMemorySpanExporter()
    provider.add_span_processor(sdk_export.SimpleSpanProcessor(memory))
    exporter = OtelExporter(tracer_provider=provider)

    root = _span("root", trace_id=uuid4().hex, span_id=uuid4().hex)
    child = _span(
        "child",
        trace_id=root.trace_id,
        span_id=uuid4().hex,
        parent_span_id=root.span_id,
    )
    score = Score(
        name="verification",
        value=0.8,
        data_type="NUMERIC",
        source="VERIFIER",
        run_id=root.trace_id,
    )
    exporter.export([child, score])
    assert memory.get_finished_spans() == ()
    exporter.export([root])

    spans = {span.name: span for span in memory.get_finished_spans()}
    assert set(spans) == {"root", "child", "score:verification"}
    root_span = spans["root"]
    child_span = spans["child"]
    assert child_span.parent is not None
    assert child_span.parent.span_id == root_span.get_span_context().span_id
    assert child_span.get_span_context().trace_id == root_span.get_span_context().trace_id
    score_span = spans["score:verification"]
    assert score_span.parent is not None
    assert score_span.parent.span_id == root_span.get_span_context().span_id
    assert score_span.get_span_context().trace_id == root_span.get_span_context().trace_id
    assert score_span.attributes["score.value"] == 0.8
    assert root_span.attributes["matterloop.trace_id"] == root.trace_id
    assert child_span.attributes["matterloop.span_id"] == child.span_id
    assert child_span.attributes["matterloop.parent_span_id"] == root.span_id
    assert root_span.start_time is not None
    assert root_span.end_time is not None
    assert root_span.end_time >= root_span.start_time
