"""基于标准库 logging 的结构化事件处理器。"""

from __future__ import annotations

import json
import logging

from matterloop_core import LoopEvent

from matterloop_observability.redaction import Redactor


class StructuredLoggingHandler:
    """把 Loop 事件输出为经过脱敏的单行 JSON。"""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger("matterloop.events")
        self._redactor = redactor or Redactor()

    def __call__(self, event: LoopEvent) -> None:
        """记录一个生命周期事件。"""
        payload = {
            "event": event.event_type.value,
            "run_id": event.context.run_id,
            "status": event.context.status.value,
            "occurred_at": event.occurred_at.isoformat(),
            "detail": event.detail,
            "metadata": dict(event.context.request.metadata),
        }
        safe_payload = self._redactor.redact(payload)
        self._logger.info(json.dumps(safe_payload, ensure_ascii=False, sort_keys=True))
