"""In-code workflow registry + the pure step-handler contract (Slice α7.2, D3.11).

This module is the **functional core** of the workflow runner. Per the sign-off
design rule (ADR-0040 / pre-flight D3.11):

    Workflow steps must be deterministic and side-effect-free.

A **step handler** is a pure function ``(StepContext) -> StepResult``. It never
performs I/O and never calls providers or external services — it *returns a
description* of what should happen: the step ``output`` to persist, the
``checkpoint_state`` to append as a resume point, and zero or more declarative
:class:`StepCommand`\\ s that a later slice (α8.x) will dispatch to real providers.
The **runner** (``AdvanceWorkflowRun`` — the imperative shell) is the only place
that touches the database, the outbox, or (eventually) a provider adapter.

Consequence: the entire runner is unit-testable with pure handlers, and swapping
in Celery/LangGraph + provider adapters later is an **execution concern, not a
domain rewrite** — the handler contract and the run/step state machines are
unchanged; only the interpreter of :class:`StepCommand` changes.

α7.2 ships **deterministic, provider-free workflows only** (there are no real
generation steps yet). They exercise the runner's full surface — success chains,
retry-then-succeed, and terminal failure — with output derived solely from the
inputs (so runs are reproducible to the byte). Real provider-backed workflows are
registered in α8.x behind this same handler protocol.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class StepOutcome(StrEnum):
    """What a pure handler decided — interpreted by the runner into DB transitions."""

    SUCCEEDED = "succeeded"
    TRANSIENT_FAILURE = "transient_failure"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True, slots=True)
class StepCommand:
    """A declarative description of an external side effect a step *wants* performed.

    Handlers are pure (D3.11): they return commands rather than executing them. In
    α7.2 nothing consumes commands (deterministic workflows produce none, or produce
    them purely for shape validation); the α8.x execution layer is what dispatches
    a command to a real provider. ``kind`` names the effect (e.g. ``"generate_image"``);
    ``args`` is its JSON-serialisable argument bag.
    """

    kind: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StepContext:
    """The pure inputs a handler is allowed to see.

    ``run_input`` is the run's frozen ``input_snapshot``; ``prior_state`` is the
    latest checkpoint state from the preceding step (the resume point), or ``None``
    for the first step; ``attempt`` is the 0-based retry attempt (0 on the first
    try, incremented on each retry) so a handler can be deterministically flaky in
    tests without any hidden state.
    """

    run_input: Mapping[str, Any]
    prior_state: Mapping[str, Any] | None
    attempt: int


@dataclass(frozen=True, slots=True)
class StepResult:
    """The pure return of a handler — a description of what should happen.

    The runner interprets ``outcome``: ``SUCCEEDED`` persists ``output`` + appends
    a checkpoint of ``checkpoint_state`` + emits ``WorkflowStepCompleted``;
    ``TRANSIENT_FAILURE`` increments ``retries`` and retries up to the definition
    bound (then fails); ``TERMINAL_FAILURE`` fails the step (and run) immediately.
    ``error`` carries the ``{code, message, …}`` payload on either failure.
    """

    outcome: StepOutcome
    output: dict[str, Any] = field(default_factory=dict)
    checkpoint_state: dict[str, Any] = field(default_factory=dict)
    commands: tuple[StepCommand, ...] = ()
    error: dict[str, Any] | None = None

    @classmethod
    def ok(
        cls,
        *,
        output: dict[str, Any] | None = None,
        checkpoint_state: dict[str, Any] | None = None,
        commands: tuple[StepCommand, ...] = (),
    ) -> StepResult:
        """A successful step."""
        return cls(
            outcome=StepOutcome.SUCCEEDED,
            output=output if output is not None else {},
            checkpoint_state=checkpoint_state if checkpoint_state is not None else {},
            commands=commands,
        )

    @classmethod
    def retry(cls, *, error: dict[str, Any]) -> StepResult:
        """A transient failure — the runner retries up to the definition bound."""
        return cls(outcome=StepOutcome.TRANSIENT_FAILURE, error=error)

    @classmethod
    def fail(cls, *, error: dict[str, Any]) -> StepResult:
        """A terminal failure — the runner fails the step (and the run) at once."""
        return cls(outcome=StepOutcome.TERMINAL_FAILURE, error=error)


class StepHandler(Protocol):
    """A pure, deterministic, side-effect-free step function (D3.11)."""

    def __call__(self, ctx: StepContext) -> StepResult: ...


@dataclass(frozen=True, slots=True)
class StepDefinition:
    """One step in a workflow definition: a name, its pure handler, and a retry bound.

    ``max_retries`` is the number of *retries* after the first attempt (so total
    attempts = ``max_retries + 1``). ``0`` means "no retries" (one attempt). No
    backoff/delay is modelled — retries are deterministic and in-process (Q5).
    """

    name: str
    handler: StepHandler
    max_retries: int = 0


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """An ordered list of steps addressed by ``key@version``."""

    key: str
    version: str
    steps: tuple[StepDefinition, ...]

    @property
    def step_specs(self) -> list[tuple[int, str]]:
        """``(step_index, step_name)`` pairs for seeding ``workflow_steps`` on create."""
        return [(i, s.name) for i, s in enumerate(self.steps)]


class WorkflowRegistry:
    """A framework-free catalogue mapping ``workflow_key@workflow_version`` → definition.

    In-code (D3.9 / Q3) — no DB-backed authoring story yet. The runner and the
    create use case both resolve definitions here; an unknown ``key@version`` is a
    ``422`` at the use-case layer. Tests may build their own isolated registry and
    inject it, so the runner never depends on the module singleton.
    """

    def __init__(self) -> None:
        self._defs: dict[tuple[str, str], WorkflowDefinition] = {}

    def register(self, definition: WorkflowDefinition) -> None:
        """Register a definition (idempotent replace on the same ``key@version``)."""
        self._defs[(definition.key, definition.version)] = definition

    def get(self, key: str, version: str) -> WorkflowDefinition | None:
        """Resolve a definition, or ``None`` if the ``key@version`` is unknown."""
        return self._defs.get((key, version))

    def keys(self) -> list[tuple[str, str]]:
        """All registered ``(key, version)`` pairs (for diagnostics/tests)."""
        return sorted(self._defs.keys())


# --------------------------------------------------------------------------- #
# α7.2 deterministic, provider-free workflows.
#
# These are pure and reproducible — output is derived only from the inputs and the
# 0-based ``attempt`` counter. They exist to exercise the runner before real
# providers arrive (α8.x); they perform NO I/O.
# --------------------------------------------------------------------------- #

# Canonical keys (kept as constants so tests and docs cannot drift from the code).
NOOP_CHAIN = "noop-chain"
RETRY_SUCCEED = "retry-succeed"
TERMINAL_FAIL = "terminal-fail"
RETRY_EXHAUST = "retry-exhaust"
WORKFLOW_VERSION_1 = "1.0.0"


def _echo_step(name: str) -> StepHandler:
    """A pure step that succeeds, echoing a deterministic view of what it saw.

    Output is a function of the run input and the prior checkpoint state only, so
    the whole chain is reproducible.
    """

    def handler(ctx: StepContext) -> StepResult:
        payload = {
            "step": name,
            "run_input": dict(ctx.run_input),
            "prior_state": dict(ctx.prior_state) if ctx.prior_state is not None else None,
        }
        return StepResult.ok(output=payload, checkpoint_state={"completed_step": name})

    return handler


def _flaky_until(name: str, *, succeed_on_attempt: int) -> StepHandler:
    """A pure step that fails transiently until ``attempt == succeed_on_attempt``.

    Deterministic because ``attempt`` is supplied by the runner (the persisted
    ``retries`` counter), not read from any clock or RNG.
    """

    def handler(ctx: StepContext) -> StepResult:
        if ctx.attempt < succeed_on_attempt:
            return StepResult.retry(
                error={
                    "code": "TRANSIENT",
                    "message": f"{name}: attempt {ctx.attempt} < {succeed_on_attempt}",
                    "attempt": ctx.attempt,
                }
            )
        return StepResult.ok(
            output={"step": name, "succeeded_on_attempt": ctx.attempt},
            checkpoint_state={"completed_step": name, "attempts": ctx.attempt},
        )

    return handler


def _always_terminal(name: str) -> StepHandler:
    """A pure step that fails terminally (no retry) — for the failure-path workflow."""

    def handler(ctx: StepContext) -> StepResult:
        return StepResult.fail(error={"code": "TERMINAL", "message": f"{name}: unrecoverable"})

    return handler


def _always_transient(name: str) -> StepHandler:
    """A pure step that always fails transiently — exhausts the retry bound."""

    def handler(ctx: StepContext) -> StepResult:
        return StepResult.retry(
            error={
                "code": "TRANSIENT",
                "message": f"{name}: attempt {ctx.attempt}",
                "attempt": ctx.attempt,
            }
        )

    return handler


def default_registry() -> WorkflowRegistry:
    """Build a registry pre-loaded with the α7.2 deterministic workflows."""
    registry = WorkflowRegistry()
    registry.register(
        WorkflowDefinition(
            key=NOOP_CHAIN,
            version=WORKFLOW_VERSION_1,
            steps=(
                StepDefinition("extract", _echo_step("extract")),
                StepDefinition("transform", _echo_step("transform")),
                StepDefinition("summarize", _echo_step("summarize")),
            ),
        )
    )
    registry.register(
        WorkflowDefinition(
            key=RETRY_SUCCEED,
            version=WORKFLOW_VERSION_1,
            steps=(
                StepDefinition("prepare", _echo_step("prepare")),
                # Fails on attempts 0 and 1, succeeds on attempt 2 (needs >= 2 retries).
                StepDefinition("flaky", _flaky_until("flaky", succeed_on_attempt=2), max_retries=3),
            ),
        )
    )
    registry.register(
        WorkflowDefinition(
            key=TERMINAL_FAIL,
            version=WORKFLOW_VERSION_1,
            steps=(
                StepDefinition("prepare", _echo_step("prepare")),
                StepDefinition("boom", _always_terminal("boom")),
            ),
        )
    )
    registry.register(
        WorkflowDefinition(
            key=RETRY_EXHAUST,
            version=WORKFLOW_VERSION_1,
            steps=(StepDefinition("doomed", _always_transient("doomed"), max_retries=2),),
        )
    )
    return registry


# Module singleton wired by the DI container. Tests can build their own via
# ``WorkflowRegistry()`` / ``default_registry()`` and inject it into the runner.
WORKFLOW_REGISTRY = default_registry()
