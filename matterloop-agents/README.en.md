[简体中文](https://github.com/huleidada/matterloop/blob/main/matterloop-agents/README.md) | English

# matterloop-agents

This package provides two layers: standard Agent components that can be injected directly into Core,
and TeamLoop, which is managed by a central controller. Both layers share model and tool protocols,
but they have different state boundaries. TeamLoop does not rely on free-form Agent-to-Agent chat to
maintain consistency.

```bash
pip install matterloop-agents
```

## Single Agent: Planner, Worker, and Verifier

```python
from matterloop_agents import (
    CriteriaVerifier,
    CriteriaVerifierConfig,
    ModelPlanner,
    ModelPlannerConfig,
    ToolCallingWorker,
    ToolCallingWorkerConfig,
)

planner = ModelPlanner(models, ModelPlannerConfig(model="planner"), memory=memory)
worker = ToolCallingWorker(
    models,
    tools,
    ToolCallingWorkerConfig(model="worker", tool_names=("filesystem",)),
)
verifier = CriteriaVerifier(models, CriteriaVerifierConfig(model="verifier"))
```

`ModelPlanner` creates bounded step plans, `ToolCallingWorker` executes tool continuation rounds, and
`CriteriaVerifier` independently determines whether a step meets its acceptance criteria.
`ModelReviewer` supports more open-ended quality review and can be converted into a Core Verifier
through its adapter.

Every model transaction pins a client through `ModelRegistry.acquire()`; hot replacement affects
only new transactions. Model JSON, plan steps, and tool arguments are still parsed strictly in the
local process. Oversized plans, invalid tools, parse failures, and exceeded tool-loop limits use typed
Agent exceptions. Core's retry policy decides whether to retry them.

<details>
<summary>Single-Agent configuration reference</summary>

- `ModelPlannerConfig(model, default_executor, max_steps, max_output_tokens, memory_namespace, memory_limit)`: defaults to executor `default`, 20 steps, 4096 output tokens, memory namespace `default`, and at most 5 memory records.
- `ToolCallingWorkerConfig(model, tool_names, max_tool_rounds, max_output_tokens)`: defaults to no tools, at most 8 rounds, and 4096 output tokens per round.
- `CriteriaVerifierConfig(model, pass_score, max_output_tokens)`: defaults to a passing score of 80 and 2048 output tokens.
- `ModelReviewerConfig(model, max_output_tokens)`: defaults to 3072 output tokens.
- `ReviewResult(score, summary, evidence, issues, recommendations)`: general review result; the adapter does not mark the result as passed when `issues` is non-empty.

These limits constrain one component call. They are not the token, cost, or cycle budget for the
entire run.

</details>

## TeamLoop: a DAG, not a group chat

```text
TeamPlanner
  └─ Creates a TaskSpec DAG from the capability snapshot, human feedback, and review history
       └─ TeamOrchestrator finds READY tasks
            ├─ Approves tasks with requires_approval
            ├─ AgentDirectory assigns an Endpoint with available capacity
            └─ Parallel execution → TaskVerifier
                                  └─ fan-in → ResultAggregator → TeamReviewer
                                               ├─ ACCEPT
                                               ├─ REPLAN
                                               ├─ REQUEST_HUMAN
                                               └─ STOP
```

`TeamOrchestrator` is the only writer of `TeamSnapshot`. An Endpoint receives an isolated
`AgentTaskContext` and returns a `TaskResult`; after fan-in, the controller commits batch results in
sequence. The official communication channel for dependent tasks is `dependency_results`. Mailbox
and ArtifactStore are optional facilities and cannot bypass the controller to mutate global state.

The Planner may select only capabilities present in the current `AgentDirectory` capability
snapshot. The task graph must be acyclic, reference existing dependencies, and use unique IDs. It is
also constrained by team concurrency and each Agent's capacity.

## Minimal team assembly

```python
from matterloop_agents.collaboration import (
    AgentDirectory,
    AgentSpec,
    AsyncTeamRuntime,
    LeastBusyScheduler,
    LoopAgentEndpoint,
    TeamOrchestrator,
    TeamOrchestratorComponents,
)

directory = AgentDirectory()
directory.register(
    LoopAgentEndpoint(
        AgentSpec("python-worker", frozenset({"python"}), max_concurrency=2),
        child_runtime,
    )
)

components = TeamOrchestratorComponents(
    planner=team_planner,
    agents=directory,
    selection_policy=LeastBusyScheduler(),
    verifier=task_verifier,
    approval_gate=approval_gate,
    repository=team_repository,
    events=team_events,
    aggregator=result_aggregator,
    reviewer=team_reviewer,
)
runtime = AsyncTeamRuntime(
    TeamOrchestrator(components, owner_id="controller-a"),
    resources=(child_runtime,),
)
```

With `reviewer=None`, every draft whose tasks passed verification is automatically accepted.
`ResultSuccessVerifier` also checks only the `success` flag. These behaviors are useful in tests, but
they are not production acceptance. A production team should configure a domain Verifier, a
whole-result Reviewer, and a real approval gate.

Model-backed team components use `ModelTeamPlannerConfig(model, max_tasks, max_output_tokens)`,
`ModelTaskVerifierConfig(model, pass_score, max_output_tokens)`,
`ModelResultAggregatorConfig(model, max_output_tokens)`, and
`ModelTeamReviewerConfig(model, pass_score, max_output_tokens)`. They store registry names only and do
not hold credentials.

## Retry, replanning, and recovery points

A task result is first saved with status `VERIFYING`, then passed to the Verifier. If the verifier
stage crashes, recovery reuses the saved result instead of re-executing an Endpoint that may have
side effects. A failed verification consumes task-level attempts first. Only after those attempts are
exhausted does the controller archive the current cycle and enter the next planning cycle with the
failure review.

`TaskSpec.replay_safe` defaults to `False`. A process interruption that leaves a task `RUNNING`
therefore enters `BLOCKED/RECOVERY_REQUIRED` for host reconciliation and does not call a replacement
Agent. Only when the host confirms that a task is read-only and safely replayable, and explicitly
sets `replay_safe=True`, does recovery release the old assignment and start the next attempt.

`TeamLimits` independently caps tasks per plan, concurrent tasks, attempts per task, team cycles,
plan revisions, and active timeout. A pause neither resets counters or usage nor contributes to the
active timeout.

`AgentDirectory.replace()` affects only new leases; existing tasks continue using the old Endpoint.
The Directory does not own the Endpoint lifecycle. The application must wait for old leases to drain
before closing old resources.

`LoopAgentEndpoint` maps a team task to a child `LoopRequest` and passes dependency results, human
feedback, and parent/child usage scopes. After merging all business metadata, it forcibly writes
`tool_access_scope=read_only`; request, task, and Endpoint metadata cannot elevate it.
`ToolCallingWorker` passes this scope to `ToolRegistry`, so a child Agent can invoke only tools
explicitly labeled `READ`. `COMPUTE`, `WRITE`, and `UNKNOWN` remain under main-Loop governance in the
default `FULL` scope. A pending interaction created by the child Loop is not
automatically promoted to the team layer. When that behavior is required, implement a dedicated
Endpoint that bridges the two HITL levels.

## Human feedback

```python
from matterloop_core import HumanAction, HumanResponse

paused = await runtime.run(request)
interaction = paused.pending_interaction
if interaction is not None:
    await runtime.submit_human_response(
        paused.run_id,
        HumanResponse(
            interaction_id=interaction.interaction_id,
            action=HumanAction.REVISE,
            content="Split the work into two parallel evidence tasks",
            idempotency_key="revision-01",
        ),
    )
    result = await runtime.resume(paused.run_id)
```

Submitting feedback writes only to the repository; it does not implicitly resume execution.
`APPROVE` continues at the exact recovery point without replaying completed tasks. `REJECT` enters
`BLOCKED/HUMAN_REJECTED`. `REVISE` and `PROVIDE_INPUT` save history and trigger replanning. Repeating
the same content with the same idempotency key is a no-op; different content under the same key raises
a conflict exception.

## Persistence and controller leases

Every Snapshot save uses version CAS. Before advancing a run, the Orchestrator also obtains a
run-level controller lease so two controllers cannot advance the same team concurrently.
`InMemoryTeamRepository` provides neither persistence nor expiring leases and is suitable only for
tests.

The current `TeamRepository` supports acquire/release but not heartbeat/renew. A production
implementation should use a lease long enough for the worst-case execution time, external fencing,
or an extended renewal protocol. If the lease expires too early, Endpoint side effects may be
repeated. CAS can prevent final-state overwrite, but it cannot undo external operations that already
occurred.

<details>
<summary>TeamLoop public data structure reference</summary>

- `TeamRequest(goal, acceptance_criteria, limits, metadata)`.
- `TeamLimits(max_tasks, max_concurrency, max_task_attempts, max_cycles, max_plan_revisions, timeout_seconds)`: defaults to 50, 4, 3, 3, 2, and no timeout.
- `AgentSpec(agent_id, capabilities, max_concurrency, version, description, role, metadata)`.
- `TaskSpec(task_id, description, capability, dependencies, acceptance_criteria, requires_approval, priority, metadata, replay_safe)`; `replay_safe` defaults to `False`, and only explicitly replay-safe pure computations are automatically invoked again after a crash.
- `AgentTaskContext(team_run_id, request, task, agent_id, attempt, dependency_results, previous_error, human_feedback)`.
- `TaskResult(task_id, agent_id, success, output, artifacts, error, attempt, metadata)`.
- `TaskVerification(passed, feedback, score, evidence, failed_criteria)`.
- `TaskState(spec, status, attempt, approval_granted, assigned_agent, result, verification, error)`.
- `TeamPlanningContext(run_id, request, cycle, plan_revision, available_agents, prior_reviews, human_feedback)`.
- `TeamReviewContext(run_id, request, cycle, plan_revision, task_results, draft_output, prior_reviews, human_feedback)`.
- `TeamReview(action, feedback, score, evidence, failed_criteria, interaction)`.
- `TeamCycleRecord(cycle, plan_revision, tasks, draft_output, review, error)`.
- `TeamSnapshot(request, tasks, run_id, status, version, stop_reason, output, error, cycle, plan_revision, cycle_history, pending_interaction, pending_review, human_interactions, review_approved_cycle, active_elapsed_seconds, active_started_at, created_at, updated_at)`.
- `TeamResult(run_id, status, task_results, output, stop_reason, error, cycle, cycle_history, pending_interaction, human_interactions, started_at, finished_at)`.
- `TeamEvent(event_type, snapshot, detail, metadata, occurred_at)`: the event carries the complete Snapshot at that point and may be large and sensitive.
- `AgentMessage(team_run_id, sender_agent_id, recipient_agent_id, message_type, content, correlation_id, metadata, message_id, created_at)`: optional Mailbox DTO; it is not a global-state channel.
- `TeamOrchestratorComponents(planner, agents, selection_policy, verifier, approval_gate, repository, events, aggregator, reviewer)`.

`TeamReviewAction` is `ACCEPT/REPLAN/REQUEST_HUMAN/STOP`. Team stop reasons distinguish completion,
approval/human rejection, task failure, no available Agent, capacity, deadlock, cancellation, timeout,
cycle/revision limits, budget exhaustion, recovery reconciliation, and component errors.

</details>

## Production boundaries

- Child Agents propose work and read evidence only. Computation, tool writes, and business writes
  return to the main Loop's approval, budget, and audit path.

- Endpoints, tools, and business writes must use team run/task/attempt as idempotency keys.
- `ResourceLimitExceededError` maps to `BLOCKED/BUDGET_EXHAUSTED`; it is not retried as an ordinary task error.
- Team events may contain the goal, complete output, human feedback, and metadata. Publishers and Repositories must enforce tenant isolation, encryption, retention, and redaction.
- `AsyncTeamRuntime` closes only objects listed in `resources`; it does not take ownership of the Directory, models, repository, or event backend.
- `LocalTeamRuntime` uses a dedicated event-loop thread. It must be closed, and it cannot call itself synchronously from that thread.

See the [architecture documentation](https://github.com/huleidada/matterloop/blob/main/docs/architecture.en.md)
for complete state and module boundaries, and the
[enterprise integration guide](https://github.com/huleidada/matterloop/blob/main/docs/enterprise-integration.en.md)
for cross-process deployment.
