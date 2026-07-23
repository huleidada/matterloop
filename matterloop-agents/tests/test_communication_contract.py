"""Agent 契约的模式校验、版本规则、兼容性与注册表测试。"""

from __future__ import annotations

import pytest
from matterloop_agents.communication.contract import (
    AgentContract,
    ContractAlreadyRegisteredError,
    ContractNotFoundError,
    ContractRegistry,
    ContractViolationError,
    SchemaSpec,
    parse_semantic_version,
    validate_payload,
)

_STRING = SchemaSpec(type="string")
_INTEGER = SchemaSpec(type="integer")
_NUMBER = SchemaSpec(type="number")
_BOOLEAN = SchemaSpec(type="boolean")


def _object(properties: dict[str, SchemaSpec], required: tuple[str, ...] = ()) -> SchemaSpec:
    """构造 object 模式的测试辅助函数。"""
    return SchemaSpec(type="object", properties=properties, required=required)


def _contract(
    *,
    agent_name: str = "planner",
    version: str = "1.0.0",
    input_required: tuple[str, ...] = ("goal",),
) -> AgentContract:
    """构造带必填输入字段的契约测试辅助函数。"""
    properties = {name: _STRING for name in input_required}
    return AgentContract(
        agent_name=agent_name,
        version=version,
        input_schema=_object(properties, required=input_required),
        output_schema=_object({"result": _STRING}, required=("result",)),
    )


class TestSchemaSpec:
    def test_rejects_unknown_type(self) -> None:
        with pytest.raises(ValueError, match="schema type"):
            SchemaSpec(type="null")

    def test_rejects_required_field_missing_from_properties(self) -> None:
        with pytest.raises(ValueError, match="required fields"):
            SchemaSpec(type="object", properties={}, required=("name",))


class TestValidatePayload:
    @pytest.mark.parametrize(
        ("schema", "payload"),
        [
            (_STRING, "hello"),
            (_INTEGER, 42),
            (_NUMBER, 3.14),
            (_NUMBER, 7),
            (_BOOLEAN, True),
            (SchemaSpec(type="array", items=_INTEGER), [1, 2, 3]),
            (_object({"name": _STRING}, required=("name",)), {"name": "matterloop"}),
        ],
    )
    def test_accepts_matching_payloads(self, schema: SchemaSpec, payload: object) -> None:
        assert validate_payload(schema, payload) == ()

    @pytest.mark.parametrize(
        ("schema", "payload"),
        [
            (_STRING, 42),
            (_INTEGER, "42"),
            (_INTEGER, True),
            (_NUMBER, True),
            (_NUMBER, "3.14"),
            (_BOOLEAN, 1),
            (SchemaSpec(type="array", items=_INTEGER), "not-a-list"),
            (_object({}), ["not", "a", "mapping"]),
        ],
    )
    def test_reports_type_mismatch(self, schema: SchemaSpec, payload: object) -> None:
        violations = validate_payload(schema, payload)
        assert len(violations) == 1
        assert violations[0].startswith("$:")

    def test_reports_missing_required_field(self) -> None:
        schema = _object({"name": _STRING, "age": _INTEGER}, required=("name", "age"))
        violations = validate_payload(schema, {"name": "loop"})
        assert violations == ("$: missing required field 'age'",)

    def test_reports_enum_violation(self) -> None:
        schema = SchemaSpec(type="string", enum=("red", "green"))
        violations = validate_payload(schema, "blue")
        assert len(violations) == 1
        assert "enum" in violations[0]
        assert validate_payload(schema, "red") == ()

    def test_reports_nested_paths_for_arrays_and_objects(self) -> None:
        item_schema = _object({"name": _STRING}, required=("name",))
        schema = _object({"body": _object({"items": SchemaSpec(type="array", items=item_schema)})})
        payload = {"body": {"items": [{"name": "a"}, {"name": "b"}, {"name": 3}]}}
        violations = validate_payload(schema, payload)
        assert violations == ("body.items[2].name: expected type string, got int",)

    def test_reports_missing_required_in_nested_array_item(self) -> None:
        item_schema = _object({"name": _STRING}, required=("name",))
        schema = _object({"items": SchemaSpec(type="array", items=item_schema)})
        violations = validate_payload(schema, {"items": [{}, {"name": "ok"}]})
        assert violations == ("items[0]: missing required field 'name'",)

    def test_collects_multiple_violations(self) -> None:
        schema = _object({"name": _STRING, "age": _INTEGER}, required=("name", "age"))
        violations = validate_payload(schema, {"name": 1})
        assert len(violations) == 2


class TestAgentContract:
    @pytest.mark.parametrize("version", ["1", "1.2", "1.2.3.4", "v1.2.3", "1.a.3", ""])
    def test_rejects_invalid_semantic_version(self, version: str) -> None:
        with pytest.raises(ValueError, match="X.Y.Z"):
            _contract(version=version)

    def test_parse_semantic_version_returns_numeric_tuple(self) -> None:
        assert parse_semantic_version("2.10.3") == (2, 10, 3)

    def test_rejects_empty_agent_name(self) -> None:
        with pytest.raises(ValueError, match="agent_name"):
            _contract(agent_name="  ")

    def test_validate_input_raises_with_violations(self) -> None:
        contract = _contract(input_required=("goal",))
        with pytest.raises(ContractViolationError) as exc_info:
            contract.validate_input({})
        assert exc_info.value.violations == ("$: missing required field 'goal'",)

    def test_validate_input_accepts_valid_payload(self) -> None:
        _contract(input_required=("goal",)).validate_input({"goal": "synthesize"})

    def test_validate_output_raises_with_violations(self) -> None:
        contract = _contract()
        with pytest.raises(ContractViolationError) as exc_info:
            contract.validate_output({"result": 1})
        assert exc_info.value.violations == ("result: expected type string, got int",)

    def test_validate_output_accepts_valid_payload(self) -> None:
        _contract().validate_output({"result": "done"})


class TestContractCompatibility:
    def test_compatible_when_other_requires_subset(self) -> None:
        current = _contract(version="1.2.0", input_required=("goal", "context"))
        other = _contract(version="1.5.9", input_required=("goal",))
        assert current.is_compatible_with(other)

    def test_compatible_with_identical_required(self) -> None:
        current = _contract(version="1.0.0", input_required=("goal",))
        other = _contract(version="1.9.9", input_required=("goal",))
        assert current.is_compatible_with(other)

    def test_incompatible_when_other_requires_more(self) -> None:
        current = _contract(version="1.0.0", input_required=("goal",))
        other = _contract(version="1.1.0", input_required=("goal", "context"))
        assert not current.is_compatible_with(other)

    def test_incompatible_across_major_versions(self) -> None:
        current = _contract(version="1.0.0")
        other = _contract(version="2.0.0")
        assert not current.is_compatible_with(other)

    def test_incompatible_across_agent_names(self) -> None:
        current = _contract(agent_name="planner")
        other = _contract(agent_name="reviewer")
        assert not current.is_compatible_with(other)


class TestContractRegistry:
    def test_register_and_get(self) -> None:
        registry = ContractRegistry()
        contract = _contract(version="1.0.0")
        registry.register(contract)
        assert registry.get("planner", "1.0.0") is contract

    def test_duplicate_registration_raises(self) -> None:
        registry = ContractRegistry()
        registry.register(_contract(version="1.0.0"))
        with pytest.raises(ContractAlreadyRegisteredError):
            registry.register(_contract(version="1.0.0"))

    def test_get_unknown_raises(self) -> None:
        registry = ContractRegistry()
        with pytest.raises(ContractNotFoundError):
            registry.get("planner", "1.0.0")

    def test_latest_orders_versions_numerically(self) -> None:
        registry = ContractRegistry()
        registry.register(_contract(version="1.9.0"))
        registry.register(_contract(version="1.10.0"))
        registry.register(_contract(version="1.2.0"))
        assert registry.latest("planner").version == "1.10.0"

    def test_latest_unknown_agent_raises(self) -> None:
        registry = ContractRegistry()
        with pytest.raises(ContractNotFoundError):
            registry.latest("missing")

    def test_versions_sorted_ascending(self) -> None:
        registry = ContractRegistry()
        registry.register(_contract(version="1.10.0"))
        registry.register(_contract(version="1.2.0"))
        assert registry.versions("planner") == ("1.2.0", "1.10.0")
