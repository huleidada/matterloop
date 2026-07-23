"""Agent 契约：最小 JSON Schema 子集校验、语义化版本与契约注册表。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from threading import Lock
from types import MappingProxyType

_ALLOWED_SCHEMA_TYPES: frozenset[str] = frozenset(
    {"object", "string", "number", "integer", "boolean", "array"}
)
_SEMVER_PATTERN: re.Pattern[str] = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class CommunicationError(Exception):
    """通信子包所有异常的基类。"""


class ContractViolationError(CommunicationError):
    """载荷不满足契约声明的输入或输出模式。

    Args:
        message: 面向调用方的违规概述。
        violations: 每条形如 ``path: reason`` 的违规消息。
    """

    def __init__(self, message: str, violations: tuple[str, ...]) -> None:
        super().__init__(message)
        self.violations: tuple[str, ...] = violations


class ContractNotFoundError(CommunicationError):
    """契约注册表中不存在指定的 Agent 契约。"""


class ContractAlreadyRegisteredError(CommunicationError):
    """同名同版本的契约已经注册。"""


@dataclass(frozen=True, slots=True)
class SchemaSpec:
    """最小 JSON Schema 子集的不可变模式节点。

    Args:
        type: 模式类型，取值为 ``object``、``string``、``number``、``integer``、
            ``boolean`` 或 ``array``。
        properties: ``object`` 类型的字段名到子模式映射。
        required: ``object`` 类型的必填字段名。
        items: ``array`` 类型的元素模式。
        enum: 允许取值的封闭集合；为 ``None`` 时不做枚举约束。
        description: 面向阅读者的模式说明。
    """

    type: str
    properties: Mapping[str, SchemaSpec] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    items: SchemaSpec | None = None
    enum: tuple[object, ...] | None = None
    description: str = ""

    def __post_init__(self) -> None:
        """校验模式类型并冻结字段映射。"""
        if self.type not in _ALLOWED_SCHEMA_TYPES:
            allowed = ", ".join(sorted(_ALLOWED_SCHEMA_TYPES))
            raise ValueError(f"schema type must be one of {{{allowed}}}, got: {self.type!r}")
        unknown_required = tuple(name for name in self.required if name not in self.properties)
        if self.type == "object" and unknown_required:
            raise ValueError(f"required fields missing from properties: {unknown_required}")
        object.__setattr__(self, "properties", MappingProxyType(dict(self.properties)))


def validate_payload(schema: SchemaSpec, payload: object) -> tuple[str, ...]:
    """按最小 JSON Schema 子集校验载荷。

    校验内容包括类型匹配、``object`` 必填字段、``array`` 元素模式和枚举取值。

    Args:
        schema: 载荷需要满足的模式。
        payload: 任意待校验对象。

    Returns:
        按发现顺序排列的违规消息元组；空元组表示校验通过。消息带有形如
        ``body.items[2].name`` 的字段路径，根路径记作 ``$``。
    """
    violations: list[str] = []
    _validate_node(schema, payload, "", violations)
    return tuple(violations)


def _validate_node(
    schema: SchemaSpec,
    payload: object,
    path: str,
    violations: list[str],
) -> None:
    """递归校验单个模式节点并把违规追加到列表。"""
    label = path or "$"
    if not _matches_type(schema.type, payload):
        violations.append(f"{label}: expected type {schema.type}, got {type(payload).__name__}")
        return
    if schema.enum is not None and payload not in schema.enum:
        violations.append(f"{label}: value {payload!r} is not one of enum {schema.enum!r}")
        return
    if schema.type == "object":
        assert isinstance(payload, Mapping)
        for name in schema.required:
            if name not in payload:
                violations.append(f"{label}: missing required field {name!r}")
        for name, sub_schema in schema.properties.items():
            if name in payload:
                _validate_node(sub_schema, payload[name], _child_path(path, name), violations)
    elif schema.type == "array" and schema.items is not None:
        assert isinstance(payload, Sequence)
        for index, item in enumerate(payload):
            _validate_node(schema.items, item, f"{path or '$'}[{index}]", violations)


def _matches_type(schema_type: str, payload: object) -> bool:
    """判断载荷是否满足指定的模式类型。"""
    if schema_type == "object":
        return isinstance(payload, Mapping)
    if schema_type == "string":
        return isinstance(payload, str)
    if schema_type == "boolean":
        return isinstance(payload, bool)
    if schema_type == "integer":
        return isinstance(payload, int) and not isinstance(payload, bool)
    if schema_type == "number":
        return isinstance(payload, (int, float)) and not isinstance(payload, bool)
    return (
        isinstance(payload, Sequence)
        and not isinstance(payload, (str, bytes, bytearray))
        and not isinstance(payload, Mapping)
    )


def _child_path(path: str, name: str) -> str:
    """拼接对象字段路径。"""
    return f"{path}.{name}" if path else name


def parse_semantic_version(version: str) -> tuple[int, int, int]:
    """解析 ``X.Y.Z`` 语义化版本字符串。

    Args:
        version: 待解析的版本字符串。

    Returns:
        ``(major, minor, patch)`` 数值元组。

    Raises:
        ValueError: 版本不符合 ``X.Y.Z`` 格式。
    """
    match = _SEMVER_PATTERN.match(version)
    if match is None:
        raise ValueError(f"version must match X.Y.Z semantic format, got: {version!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


@dataclass(frozen=True, slots=True)
class AgentContract:
    """描述一个 Agent 输入输出模式的不可变契约。

    Args:
        agent_name: 契约所属 Agent 的稳定名称。
        version: ``X.Y.Z`` 语义化版本字符串，构造时校验格式。
        input_schema: Agent 接受输入的模式。
        output_schema: Agent 产出结果的模式。
        description: 面向阅读者的契约说明。
    """

    agent_name: str
    version: str
    input_schema: SchemaSpec
    output_schema: SchemaSpec
    description: str = ""

    def __post_init__(self) -> None:
        """校验名称非空与版本格式。"""
        if not self.agent_name.strip():
            raise ValueError("agent_name must not be empty")
        parse_semantic_version(self.version)

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        """返回按数值比较的 ``(major, minor, patch)`` 版本元组。"""
        return parse_semantic_version(self.version)

    def validate_input(self, payload: object) -> None:
        """校验输入载荷是否满足输入模式。

        Args:
            payload: 待校验的输入载荷。

        Raises:
            ContractViolationError: 载荷存在一处或多处违规。
        """
        self._validate("input", self.input_schema, payload)

    def validate_output(self, payload: object) -> None:
        """校验输出载荷是否满足输出模式。

        Args:
            payload: 待校验的输出载荷。

        Raises:
            ContractViolationError: 载荷存在一处或多处违规。
        """
        self._validate("output", self.output_schema, payload)

    def is_compatible_with(self, other: AgentContract) -> bool:
        """判断当前契约的调用方能否安全切换到 ``other`` 契约。

        兼容规则：两个契约的 ``agent_name`` 相同、major 版本相同，并且 ``other``
        输入模式的必填字段是当前契约输入必填字段的子集。必填字段收窄意味着按当前
        契约构造的输入天然满足 ``other``，即消费者可以接受更宽输入。

        Args:
            other: 待评估的目标契约。

        Returns:
            调用方可以无缝迁移到 ``other`` 时为 ``True``。
        """
        if self.agent_name != other.agent_name:
            return False
        if self.version_tuple[0] != other.version_tuple[0]:
            return False
        return set(other.input_schema.required).issubset(set(self.input_schema.required))

    def _validate(self, direction: str, schema: SchemaSpec, payload: object) -> None:
        """执行模式校验并在违规时抛出契约异常。"""
        violations = validate_payload(schema, payload)
        if violations:
            raise ContractViolationError(
                f"{direction} payload violates contract"
                f" {self.agent_name}@{self.version}: {len(violations)} violation(s)",
                violations,
            )


class ContractRegistry:
    """按 ``(agent_name, version)`` 管理 Agent 契约的线程安全注册表。"""

    def __init__(self) -> None:
        self._contracts: dict[tuple[str, str], AgentContract] = {}
        self._lock = Lock()

    def register(self, contract: AgentContract) -> None:
        """注册一份契约。

        Args:
            contract: 待注册的不可变契约。

        Raises:
            ContractAlreadyRegisteredError: 同名同版本的契约已经存在。
        """
        key = (contract.agent_name, contract.version)
        with self._lock:
            if key in self._contracts:
                raise ContractAlreadyRegisteredError(
                    f"contract is already registered: {contract.agent_name}@{contract.version}"
                )
            self._contracts[key] = contract

    def get(self, agent_name: str, version: str) -> AgentContract:
        """按名称与版本查询契约。

        Args:
            agent_name: Agent 名称。
            version: ``X.Y.Z`` 版本字符串。

        Returns:
            匹配的契约。

        Raises:
            ContractNotFoundError: 指定名称与版本的契约不存在。
        """
        with self._lock:
            contract = self._contracts.get((agent_name, version))
        if contract is None:
            raise ContractNotFoundError(f"contract is not registered: {agent_name}@{version}")
        return contract

    def latest(self, agent_name: str) -> AgentContract:
        """返回指定 Agent 的最高版本契约。

        版本比较按 ``(major, minor, patch)`` 数值元组进行，而非字符串顺序。

        Args:
            agent_name: Agent 名称。

        Returns:
            版本最高的契约。

        Raises:
            ContractNotFoundError: 该 Agent 没有任何已注册契约。
        """
        with self._lock:
            candidates = [
                contract for (name, _), contract in self._contracts.items() if name == agent_name
            ]
        if not candidates:
            raise ContractNotFoundError(f"no contract is registered for agent: {agent_name}")
        return max(candidates, key=lambda contract: contract.version_tuple)

    def versions(self, agent_name: str) -> tuple[str, ...]:
        """返回指定 Agent 按版本升序排列的全部已注册版本。

        Args:
            agent_name: Agent 名称。

        Returns:
            升序版本字符串元组；没有注册时为空元组。
        """
        with self._lock:
            found = [
                contract for (name, _), contract in self._contracts.items() if name == agent_name
            ]
        return tuple(
            contract.version
            for contract in sorted(found, key=lambda contract: contract.version_tuple)
        )


__all__ = [
    "AgentContract",
    "CommunicationError",
    "ContractAlreadyRegisteredError",
    "ContractNotFoundError",
    "ContractRegistry",
    "ContractViolationError",
    "SchemaSpec",
    "parse_semantic_version",
    "validate_payload",
]
