"""Repeatable, fixed-plan retrieval experiments for provider comparisons.

The Planner is deliberately outside this module: every provider is handed the
same already-built :class:`ResearchPlan`.  That prevents planner variation
from being misreported as retrieval-provider variation.
"""

from __future__ import annotations

from hashlib import sha256
from time import perf_counter
from uuid import uuid4

from pydantic import BaseModel, Field

from .gateway import RetrievalGateway, make_gateway, run_plan
from .models import EvidenceRecord, ResearchPlan, ResearchReport
from .multi_provider_factory import make_multi_provider_gateway
from .pipeline import run_research
from .telemetry import ExperimentContext, SourceAttempt, TelemetryEmitter, TelemetryRecorder

__all__ = [
    "ExperimentRun",
    "FixedPlanExperiment",
    "EndToEndExperimentRun",
    "EndToEndFixedPlanExperiment",
    "run_fixed_plan_experiment",
    "run_end_to_end_fixed_plan_experiment",
]


class ExperimentRun(BaseModel):
    """Evidence and timings from one comparable retrieval execution."""

    mode: str
    providers: list[str]
    duration_ms: float
    records: list[EvidenceRecord]
    attempts: list[SourceAttempt]

    @property
    def successful_records(self) -> int:
        return sum(record.status == "success" for record in self.records)


class FixedPlanExperiment(BaseModel):
    """Results for independent provider baselines plus an optional fan-out run."""

    experiment_id: str
    scenario: str
    plan_hash: str
    runs: list[ExperimentRun] = Field(default_factory=list)


class EndToEndExperimentRun(BaseModel):
    """One fixed-plan retrieval arm passed through ValidatorNode and WriterNode."""

    mode: str
    providers: list[str]
    duration_ms: float
    report: ResearchReport


class EndToEndFixedPlanExperiment(BaseModel):
    """Comparable grounded-answer results for provider baselines and fan-out."""

    experiment_id: str
    scenario: str
    plan_hash: str
    writer_model: str
    runs: list[EndToEndExperimentRun] = Field(default_factory=list)


async def run_fixed_plan_experiment(
    plan: ResearchPlan,
    *,
    scenario: str,
    model_map: dict[str, str],
    include_fanout: bool = True,
    emit_telemetry: bool = False,
    experiment_id: str | None = None,
) -> FixedPlanExperiment:
    """Run every provider against the same fixed plan, then optionally fan out.

    Individual runs establish provider baselines. The fan-out run establishes
    the combined system's resilience and wall-clock behavior. Each run gets a
    distinct Logfire scenario while sharing one experiment id and plan hash.
    """
    if not model_map:
        raise ValueError("model_map must include at least one provider")

    plan = plan.model_copy(update={"retrieval_backend": "pydantic_native"})
    providers = list(model_map)
    plan_hash = sha256(plan.model_dump_json().encode()).hexdigest()
    experiment_id = experiment_id or f"provider-comparison-{uuid4().hex[:8]}"
    runs: list[ExperimentRun] = []

    for provider, model in model_map.items():
        recorder = _recorder(
            experiment_id, scenario, plan_hash, [model], f"{scenario}:single:{provider}", emit_telemetry
        )
        gateway = make_gateway(
            plan,
            model=model,
            telemetry_provider=provider,
            telemetry_recorder=recorder,
        )
        runs.append(await _run_gateway(gateway, plan, recorder, mode="single", providers=[provider]))

    if include_fanout and len(providers) > 1:
        recorder = _recorder(
            experiment_id, scenario, plan_hash, list(model_map.values()), f"{scenario}:fanout", emit_telemetry
        )
        gateway = make_multi_provider_gateway(
            plan,
            providers=providers,
            model_map=model_map,
            telemetry_recorder=recorder,
        )
        runs.append(await _run_gateway(gateway, plan, recorder, mode="fanout", providers=providers))

    return FixedPlanExperiment(
        experiment_id=experiment_id,
        scenario=scenario,
        plan_hash=plan_hash,
        runs=runs,
    )


async def run_end_to_end_fixed_plan_experiment(
    plan: ResearchPlan,
    *,
    scenario: str,
    model_map: dict[str, str],
    writer_model: str,
    include_fanout: bool = True,
    emit_telemetry: bool = False,
    experiment_id: str | None = None,
) -> EndToEndFixedPlanExperiment:
    """Compare grounded outputs while holding both plan and Writer model fixed."""
    if not model_map:
        raise ValueError("model_map must include at least one provider")

    plan = plan.model_copy(update={"retrieval_backend": "pydantic_native"})
    plan_hash = sha256(plan.model_dump_json().encode()).hexdigest()
    experiment_id = experiment_id or f"provider-e2e-{uuid4().hex[:8]}"
    configurations = [("single", [provider]) for provider in model_map]
    if include_fanout and len(model_map) > 1:
        configurations.append(("fanout", list(model_map)))

    runs: list[EndToEndExperimentRun] = []
    for mode, providers in configurations:
        selected_models = {provider: model_map[provider] for provider in providers}
        experiment = ExperimentContext(
            experiment_id=experiment_id,
            scenario=f"{scenario}:{mode}:{'+'.join(providers)}",
            plan_hash=plan_hash,
            configured_backends=["pydantic_native"],
            configured_models=list(selected_models.values()) + [writer_model],
        )
        started = perf_counter()
        report = await run_research(
            plan.question,
            model=writer_model,
            retrieval_backend="pydantic_native",
            enable_otel_tracing=emit_telemetry,
            multi_provider=providers,
            model_map=selected_models,
            fixed_plan=plan,
            telemetry_experiment=experiment,
        )
        runs.append(
            EndToEndExperimentRun(
                mode=mode,
                providers=providers,
                duration_ms=(perf_counter() - started) * 1000,
                report=report,
            )
        )

    return EndToEndFixedPlanExperiment(
        experiment_id=experiment_id,
        scenario=scenario,
        plan_hash=plan_hash,
        writer_model=writer_model,
        runs=runs,
    )


def _recorder(
    experiment_id: str,
    scenario: str,
    plan_hash: str,
    models: list[str],
    run_scenario: str,
    emit_telemetry: bool,
) -> TelemetryRecorder:
    return TelemetryRecorder(
        experiment=ExperimentContext(
            experiment_id=experiment_id,
            scenario=run_scenario,
            plan_hash=plan_hash,
            configured_backends=["pydantic_native"],
            configured_models=models,
        ),
        emitter=TelemetryEmitter(enabled=emit_telemetry),
    )


async def _run_gateway(
    gateway: RetrievalGateway,
    plan: ResearchPlan,
    recorder: TelemetryRecorder,
    *,
    mode: str,
    providers: list[str],
) -> ExperimentRun:
    started = perf_counter()
    async with gateway:
        records = await run_plan(gateway, plan)
    return ExperimentRun(
        mode=mode,
        providers=providers,
        duration_ms=(perf_counter() - started) * 1000,
        records=records,
        attempts=recorder.attempts,
    )
