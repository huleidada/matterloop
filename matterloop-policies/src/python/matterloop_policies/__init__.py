"""MatterLoop 策略组件公共 API。"""

from matterloop_policies.approval import AllowAllApproval, ApprovalRule, RuleBasedApprovalGate
from matterloop_policies.budget import BudgetLimits, BudgetPolicy, CompositeLoopPolicy
from matterloop_policies.errors import (
    BudgetConfigurationError,
    BudgetError,
    ResourceLimitExceededError,
    UsageReservationError,
)
from matterloop_policies.metering import (
    BudgetedModelClient,
    ModelInputTokenEstimator,
    TokenRateCard,
    estimate_utf8_input_tokens,
)
from matterloop_policies.permissions import PermissionRule, RuleBasedPermissionPolicy
from matterloop_policies.retry import ExponentialBackoffRetryPolicy, RetryConfig
from matterloop_policies.security import (
    AuthenticationError,
    Authenticator,
    AuthorizationError,
    Authorizer,
    DataAccessPolicy,
    DataAccessRule,
    Grant,
    Identity,
    RoleBasedAuthorizer,
    StaticTokenAuthenticator,
)
from matterloop_policies.stop import NoProgressStopPolicy, StopConfig
from matterloop_policies.tenancy import (
    TenancyError,
    TenantContext,
    TenantInactiveError,
    TenantIsolationError,
    TenantIsolationPolicy,
    TenantLimitsFactory,
    TenantNotFoundError,
    TenantRegistry,
    TenantScopedLedgers,
)
from matterloop_policies.usage import UsageAmount, UsageLedger, UsageReservation, UsageSnapshot
from matterloop_policies.wrappers import (
    BudgetedAgentEndpoint,
    BudgetedExecutor,
    BudgetedTool,
    ScopeResolver,
)

__all__ = [
    "AllowAllApproval",
    "ApprovalRule",
    "AuthenticationError",
    "Authenticator",
    "AuthorizationError",
    "Authorizer",
    "BudgetConfigurationError",
    "BudgetError",
    "BudgetLimits",
    "BudgetPolicy",
    "BudgetedAgentEndpoint",
    "BudgetedExecutor",
    "BudgetedModelClient",
    "BudgetedTool",
    "CompositeLoopPolicy",
    "DataAccessPolicy",
    "DataAccessRule",
    "ExponentialBackoffRetryPolicy",
    "Grant",
    "Identity",
    "NoProgressStopPolicy",
    "PermissionRule",
    "ResourceLimitExceededError",
    "RetryConfig",
    "RoleBasedAuthorizer",
    "RuleBasedApprovalGate",
    "RuleBasedPermissionPolicy",
    "ScopeResolver",
    "StaticTokenAuthenticator",
    "StopConfig",
    "TenancyError",
    "TenantContext",
    "TenantInactiveError",
    "TenantIsolationError",
    "TenantIsolationPolicy",
    "TenantLimitsFactory",
    "TenantNotFoundError",
    "TenantRegistry",
    "TenantScopedLedgers",
    "TokenRateCard",
    "UsageAmount",
    "UsageLedger",
    "UsageReservation",
    "UsageReservationError",
    "UsageSnapshot",
    "ModelInputTokenEstimator",
    "estimate_utf8_input_tokens",
]
