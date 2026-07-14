"""人类交互请求、响应与审计记录值对象。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from uuid import uuid4


class HumanInteractionKind(str, Enum):
    """区分人工交互发生的业务位置。"""

    APPROVAL = "approval"
    INPUT = "input"
    COMPLETION_REVIEW = "completion_review"


class HumanAction(str, Enum):
    """人类可以对待处理请求执行的标准动作。"""

    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"
    PROVIDE_INPUT = "provide_input"


@dataclass(frozen=True, slots=True)
class HumanInteractionRequest:
    """描述一次需要外部人类处理的、可持久化交互。

    Args:
        kind: 交互在 Loop 中承担的职责。
        prompt: 展示给人类的明确问题或审批说明。
        allowed_actions: 当前请求允许提交的动作集合。
        interaction_id: 跨暂停和恢复保持稳定的交互标识。
        step_id: 审批计划步骤时关联的步骤标识。
        metadata: 供 UI 或审计系统使用的只读扩展数据。
        created_at: 请求产生时间。
    """

    kind: HumanInteractionKind
    prompt: str
    allowed_actions: tuple[HumanAction, ...] = (
        HumanAction.APPROVE,
        HumanAction.REJECT,
        HumanAction.REVISE,
        HumanAction.PROVIDE_INPUT,
    )
    interaction_id: str = field(default_factory=lambda: uuid4().hex)
    step_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """校验交互边界并冻结扩展数据。"""
        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")
        if not self.interaction_id.strip():
            raise ValueError("interaction_id must not be empty")
        if not self.allowed_actions:
            raise ValueError("allowed_actions must not be empty")
        if len(self.allowed_actions) != len(set(self.allowed_actions)):
            raise ValueError("allowed_actions must not contain duplicates")
        if self.step_id is not None and not self.step_id.strip():
            raise ValueError("step_id must not be empty")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class HumanResponse:
    """保存调用方对一次人工交互提交的幂等响应。

    Args:
        interaction_id: 必须与待处理请求一致的交互标识。
        action: 人类选择的标准动作。
        content: 修改意见、拒绝原因或补充输入。
        idempotency_key: 调用方重试提交时复用的幂等键。
        metadata: 只读的响应审计信息。
        responded_at: 响应产生时间。
    """

    interaction_id: str
    action: HumanAction
    content: str = ""
    idempotency_key: str = field(default_factory=lambda: uuid4().hex)
    metadata: Mapping[str, object] = field(default_factory=dict)
    responded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """拒绝无法关联或无法幂等处理的响应。"""
        if not self.interaction_id.strip():
            raise ValueError("interaction_id must not be empty")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if self.action in {HumanAction.REVISE, HumanAction.PROVIDE_INPUT} and not (
            self.content.strip()
        ):
            raise ValueError(f"{self.action.value} requires non-empty content")
        if self.responded_at.tzinfo is None:
            raise ValueError("responded_at must include a timezone")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def feedback(self) -> str:
        """返回响应正文的兼容语义名称。"""
        return self.content


@dataclass(frozen=True, slots=True)
class HumanInteractionRecord:
    """把已处理请求和响应组合成不可变审计记录。"""

    request: HumanInteractionRequest
    response: HumanResponse
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """确保记录内部关联一致且时间可跨进程解释。"""
        if self.request.interaction_id != self.response.interaction_id:
            raise ValueError("request and response interaction_id must match")
        if self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must include a timezone")
