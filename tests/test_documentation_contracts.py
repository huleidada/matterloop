"""公开字段、文档链接和离线边界的工作区级契约测试。"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import re
from pathlib import Path
from types import ModuleType
from urllib.parse import unquote

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]

# 这些类型构成用户确认的“集成公共面”。私有实现和 continuation 内部载荷不进入字段文档。
FIELD_CONTRACTS: dict[str, dict[str, tuple[str, ...]]] = {
    "matterloop-core/README.md": {
        "matterloop_core": (
            "ArtifactRef",
            "CompletionDecision",
            "ComponentSpec",
            "ExecutionResult",
            "HumanInteractionRecord",
            "HumanInteractionRequest",
            "HumanResponse",
            "IterationRecord",
            "LoopContext",
            "LoopEvent",
            "LoopLimits",
            "LoopRequest",
            "LoopResult",
            "Plan",
            "PlanStep",
            "PluginDefinition",
            "RetryDecision",
            "VerificationResult",
        ),
    },
    "matterloop-models/README.md": {
        "matterloop_models": (
            "ModelCapabilities",
            "ModelDescriptor",
            "ModelMessage",
            "ModelRequest",
            "ModelRequirements",
            "ModelResponse",
            "TokenUsage",
            "ToolCall",
            "ToolDefinition",
            "ToolOutput",
        ),
        "matterloop_models.providers": (
            "DeepSeekModelConfig",
            "GLMModelConfig",
            "MiniMaxModelConfig",
            "OpenAICompatibleChatConfig",
            "OpenAIModelConfig",
            "QwenModelConfig",
            "ZhipuModelConfig",
        ),
    },
    "matterloop-runtime/README.md": {
        "matterloop_runtime": (
            "ProcessRequest",
            "ProcessResult",
            "QueueLease",
            "QueuedRun",
            "RunRecord",
        ),
    },
    "matterloop-tools/README.md": {
        "matterloop_tools": ("ToolContext", "ToolResult", "ToolSpec"),
    },
    "matterloop-memory/README.md": {
        "matterloop_memory": ("MemoryMatch", "MemoryQuery", "MemoryRecord"),
    },
    "matterloop-policies/README.md": {
        "matterloop_policies": (
            "ApprovalRule",
            "BudgetLimits",
            "PermissionRule",
            "RetryConfig",
            "StopConfig",
            "TokenRateCard",
            "UsageAmount",
            "UsageReservation",
            "UsageSnapshot",
        ),
    },
    "matterloop-agents/README.md": {
        "matterloop_agents": (
            "CriteriaVerifierConfig",
            "ModelPlannerConfig",
            "ModelReviewerConfig",
            "ReviewResult",
            "ToolCallingWorkerConfig",
        ),
        "matterloop_agents.collaboration": (
            "AgentMessage",
            "AgentSpec",
            "AgentTaskContext",
            "ModelResultAggregatorConfig",
            "ModelTaskVerifierConfig",
            "ModelTeamPlannerConfig",
            "ModelTeamReviewerConfig",
            "TaskResult",
            "TaskSpec",
            "TaskState",
            "TaskVerification",
            "TeamCycleRecord",
            "TeamEvent",
            "TeamLimits",
            "TeamOrchestratorComponents",
            "TeamPlanningContext",
            "TeamRequest",
            "TeamResult",
            "TeamReview",
            "TeamReviewContext",
            "TeamSnapshot",
        ),
    },
    "matterloop-presets/README.md": {
        "matterloop_presets": (
            "AgentPresetConfig",
            "CodingPresetConfig",
            "MinimalPresetConfig",
            "ProductionPresetConfig",
            "ResearchPresetConfig",
        ),
    },
    "matterloop-integration-fastapi/README.md": {
        "matterloop_integration_fastapi": (
            "ArtifactResponse",
            "CancelResponse",
            "CreateLoopRequest",
            "EventListResponse",
            "ExecutionResponse",
            "IterationResponse",
            "LoopLimitsRequest",
            "PlanStepResponse",
            "ResumeLoopRequest",
            "ResumeResponse",
            "RunResponse",
            "VerificationResponse",
        ),
    },
    "matterloop-integration-celery/README.md": {
        "matterloop_integration_celery": (
            "CeleryWorkerDependencies",
            "RegisteredCeleryTasks",
        ),
    },
    "matterloop-integration-redis/README.md": {
        "matterloop_integration_redis": ("RedisConfig",),
    },
}

SIGNATURE_CONTRACTS: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "matterloop-core/README.md": (
        (
            "matterloop_core",
            "AgentLoop",
            (
                "planners",
                "executors",
                "verifiers",
                "checkpoint_store",
                "policy",
                "events",
                "approval_gate",
                "retry_policy",
                "completion_evaluator",
            ),
        ),
    ),
    "matterloop-models/README.md": (
        (
            "matterloop_models",
            "ModelRegistry.register",
            ("name", "client", "replace", "descriptor"),
        ),
        ("matterloop_models", "ModelRegistry.acquire", ("name",)),
        ("matterloop_models", "ModelRegistry.swap", ("name", "client", "descriptor")),
    ),
    "matterloop-runtime/README.md": (
        ("matterloop_runtime", "AsyncRuntime", ("engine", "resources")),
        ("matterloop_runtime", "LocalRuntime", ("runtime", "thread_name")),
        ("matterloop_runtime", "QueueRuntime", ("producer", "repository", "event_reader")),
    ),
    "matterloop-tools/README.md": (
        ("matterloop_tools", "ToolRegistry", ("tools", "authorizer")),
        (
            "matterloop_tools",
            "FileSystemTool",
            ("root", "allow_write", "max_read_bytes", "max_write_bytes", "max_list_entries"),
        ),
        (
            "matterloop_tools",
            "ShellTool",
            (
                "workspace",
                "allowed_commands",
                "sandbox",
                "base_environment",
                "allowed_environment",
                "max_timeout_seconds",
                "max_output_bytes",
            ),
        ),
        (
            "matterloop_tools",
            "HttpTool",
            (
                "allowed_hosts",
                "allowed_methods",
                "require_https",
                "follow_redirects",
                "max_redirects",
                "max_timeout_seconds",
                "max_response_bytes",
                "max_request_bytes",
                "allowed_headers",
                "transport",
            ),
        ),
    ),
    "matterloop-observability/README.md": (
        ("matterloop_observability", "CompositeEventPublisher", ("publishers", "failure_mode")),
        ("matterloop_observability", "StructuredLoggingHandler", ("logger", "redactor")),
        ("matterloop_observability", "Redactor", ("extra_fields",)),
    ),
    "matterloop-presets/README.md": (
        ("matterloop_presets", "build_minimal_runtime", ("model", "config")),
        ("matterloop_presets", "build_coding_runtime", ("model", "workspace", "config")),
        (
            "matterloop_presets",
            "build_research_runtime",
            ("model", "workspace", "config"),
        ),
        (
            "matterloop_presets",
            "build_production_runtime",
            (
                "model",
                "config",
                "queue_backend",
                "run_repository",
                "checkpoint_store",
                "audit_publisher",
                "event_reader",
                "approval_gate",
            ),
        ),
    ),
    "matterloop-integration-fastapi/README.md": (
        (
            "matterloop_integration_fastapi",
            "create_router",
            ("runtime", "auth_dependency", "prefix"),
        ),
    ),
    "matterloop-integration-celery/README.md": (
        ("matterloop_integration_celery", "CeleryQueueProducer", ("app", "queue", "codec")),
        ("matterloop_integration_celery", "register_tasks", ("celery_app", "runtime_factory_path")),
    ),
    "matterloop-integration-redis/README.md": (
        ("matterloop_integration_redis", "RedisQueueBackend", ("client", "config", "codec")),
        ("matterloop_integration_redis", "RedisRunRepository", ("client", "config", "codec")),
        (
            "matterloop_integration_redis",
            "RedisEventPublisher",
            ("client", "config", "checkpoint_codec"),
        ),
    ),
}


def test_integration_public_fields_are_documented() -> None:
    """字段契约中的每个真实字段都必须出现在对应 README 的代码标记中。"""
    for readme_path, modules in FIELD_CONTRACTS.items():
        document = (ROOT / readme_path).read_text(encoding="utf-8")
        for module_name, class_names in modules.items():
            module = importlib.import_module(module_name)
            for class_name in class_names:
                value = getattr(module, class_name)
                assert _inside_code_span(document, class_name), (
                    f"{readme_path} 没有说明公共类型 {module_name}.{class_name}"
                )
                for field_name in _public_fields(value):
                    assert _inside_code_span(document, field_name), (
                        f"{readme_path} 没有说明 {class_name}.{field_name}"
                    )


def test_integration_entrypoint_parameters_are_documented_and_real() -> None:
    """关键构造器参数必须同时存在于代码签名和中文文档。"""
    for readme_path, contracts in SIGNATURE_CONTRACTS.items():
        document = (ROOT / readme_path).read_text(encoding="utf-8")
        for module_name, qualified_name, parameter_names in contracts:
            value = _resolve(importlib.import_module(module_name), qualified_name)
            signature = inspect.signature(value)
            for parameter_name in parameter_names:
                assert parameter_name in signature.parameters, (
                    f"契约参数已过期：{module_name}.{qualified_name}.{parameter_name}"
                )
                assert _inside_code_span(document, parameter_name), (
                    f"{readme_path} 没有说明 {qualified_name}.{parameter_name}"
                )


def test_markdown_internal_links_resolve() -> None:
    """根文档、架构文档和所有发行包 README 的相对链接必须存在。"""
    documents = (
        ROOT / "README.md",
        *(ROOT / "docs").glob("*.md"),
        *(ROOT.glob("matterloop-*/README.md")),
    )
    link_pattern = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
    for document_path in documents:
        document = document_path.read_text(encoding="utf-8")
        for raw_target in link_pattern.findall(document):
            target = raw_target.strip().strip("<>")
            if target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            target_path = unquote(target.split("#", maxsplit=1)[0])
            resolved = (document_path.parent / target_path).resolve()
            assert resolved.exists(), f"{document_path} 包含失效链接：{raw_target}"


def test_examples_do_not_read_environment_or_contain_key_material() -> None:
    """离线示例不得读取环境配置，也不得包含常见密钥前缀。"""
    forbidden_patterns = (
        re.compile(r"os\.environ"),
        re.compile(r"os\.getenv"),
        re.compile(r"load_dotenv"),
        re.compile(r"dotenv_values"),
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}"),
    )
    for example_path in (ROOT / "examples" / "enterprise").glob("*.py"):
        source = example_path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            assert pattern.search(source) is None, f"{example_path} 包含禁止内容：{pattern.pattern}"


def _public_fields(value: object) -> tuple[str, ...]:
    """读取 dataclass 或 Pydantic 公共字段名称。"""
    if inspect.isclass(value) and dataclasses.is_dataclass(value):
        return tuple(field.name for field in dataclasses.fields(value))
    if inspect.isclass(value) and issubclass(value, BaseModel):
        return tuple(value.model_fields)
    raise TypeError(f"unsupported documentation contract type: {value!r}")


def _inside_code_span(document: str, name: str) -> bool:
    """判断名称是否出现在 Markdown 行内代码标记中。"""
    pattern = re.compile(rf"`[^`\n]*\b{re.escape(name)}\b[^`\n]*`")
    return pattern.search(document) is not None


def _resolve(module: ModuleType, qualified_name: str) -> object:
    """从公共模块解析类、函数或方法。"""
    value: object = module
    for part in qualified_name.split("."):
        value = getattr(value, part)
    return value
