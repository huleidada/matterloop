"""Agent 通信模型：契约校验、异步消息总线与管理面注册表。"""

from __future__ import annotations

from matterloop_agents.communication.bus import (
    AgentEventMessage,
    AgentMessageBus,
    AgentRequest,
    AgentResponse,
    BroadcastMessage,
    BusMessage,
    MessageBusBackpressureError,
    MessageBusError,
    RequestTimeoutError,
    UnknownCorrelationError,
    UnknownRecipientError,
)
from matterloop_agents.communication.contract import (
    AgentContract,
    CommunicationError,
    ContractAlreadyRegisteredError,
    ContractNotFoundError,
    ContractRegistry,
    ContractViolationError,
    SchemaSpec,
    parse_semantic_version,
    validate_payload,
)
from matterloop_agents.communication.registry import (
    AgentAlreadyRegisteredError,
    AgentNotRegisteredError,
    AgentRegistration,
    AgentRuntimeStatus,
    AgentSla,
    InvalidStatusTransitionError,
    ManagedAgentRegistry,
    RegistryError,
)

__all__ = [
    "AgentAlreadyRegisteredError",
    "AgentContract",
    "AgentEventMessage",
    "AgentMessageBus",
    "AgentNotRegisteredError",
    "AgentRegistration",
    "AgentRequest",
    "AgentResponse",
    "AgentRuntimeStatus",
    "AgentSla",
    "BroadcastMessage",
    "BusMessage",
    "CommunicationError",
    "ContractAlreadyRegisteredError",
    "ContractNotFoundError",
    "ContractRegistry",
    "ContractViolationError",
    "InvalidStatusTransitionError",
    "ManagedAgentRegistry",
    "MessageBusBackpressureError",
    "MessageBusError",
    "RegistryError",
    "RequestTimeoutError",
    "SchemaSpec",
    "UnknownCorrelationError",
    "UnknownRecipientError",
    "parse_semantic_version",
    "validate_payload",
]
