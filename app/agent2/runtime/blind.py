from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

from app.agent2.cognitive_core_v3 import SemanticInterpreter
from app.agent2.conversation_state import ConversationState
from app.agent2.conversation_state_store import InMemoryConversationStateStore
from app.agent2.oracle_guard import assert_no_oracle_fields
from app.agent2.json_immutability import freeze_json_value, thaw_json_value
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot

from .composition import compose_phase1_runtime
from .contracts import (
    InMemoryRuntimeAuditSink,
    RuntimeActor,
    RuntimeFailureOutcome,
    RuntimeTurnOutcome,
    RuntimeTurnRequest,
)
from .domains import InMemoryDailyDomainExecutor


BLIND_INPUT_SCHEMA_VERSION = "agent2.runtime_blind_input.v1"
BLIND_ACTUAL_SCHEMA_VERSION = "agent2.runtime_actual_artifact.v1"

_PACK_FIELDS = frozenset({"schema_version", "pack_id", "cases", "digest"})
_CASE_FIELDS = frozenset(
    {
        "case_id",
        "actor_id",
        "conversation_id",
        "initial_state",
        "initial_daily_snapshot",
        "runtime_config",
        "turns",
    }
)
_TURN_FIELDS = frozenset(
    {"turn_id", "raw_text", "occurred_at", "channel", "request_metadata"}
)


@dataclass(frozen=True)
class BlindTurnInput:
    turn_id: str
    raw_text: str
    occurred_at: datetime
    channel: str
    request_metadata: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BlindTurnInput":
        _require_closed_fields(payload, _TURN_FIELDS, context="blind turn")
        occurred_at = _datetime(payload.get("occurred_at"), context="blind turn occurred_at")
        request_metadata = payload.get("request_metadata") or {}
        if not isinstance(request_metadata, Mapping):
            raise ValueError("blind turn request_metadata must be an object")
        turn = cls(
            turn_id=str(payload.get("turn_id") or "").strip(),
            raw_text=str(payload.get("raw_text") or ""),
            occurred_at=occurred_at,
            channel=str(payload.get("channel") or "").strip(),
            request_metadata=freeze_json_value(request_metadata, path="blind turn request_metadata"),
        )
        if not turn.turn_id or not turn.raw_text.strip() or not turn.channel:
            raise ValueError("blind turn requires id, raw text, timestamp, and channel")
        return turn

    def as_mapping(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "raw_text": self.raw_text,
            "occurred_at": self.occurred_at.isoformat(),
            "channel": self.channel,
            "request_metadata": thaw_json_value(self.request_metadata),
        }


@dataclass(frozen=True)
class BlindRuntimeCase:
    case_id: str
    actor_id: UUID
    conversation_id: str
    initial_state: Mapping[str, Any] | None
    initial_daily_snapshot: Mapping[str, Any]
    runtime_config: Mapping[str, Any]
    turns: tuple[BlindTurnInput, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BlindRuntimeCase":
        _require_closed_fields(payload, _CASE_FIELDS, context="blind case")
        state = payload.get("initial_state")
        if state is not None and not isinstance(state, Mapping):
            raise ValueError("blind case initial_state must be an object or null")
        snapshot = payload.get("initial_daily_snapshot")
        runtime_config = payload.get("runtime_config")
        raw_turns = payload.get("turns")
        if not isinstance(snapshot, Mapping) or not isinstance(runtime_config, Mapping):
            raise ValueError("blind case requires snapshot and runtime config objects")
        if not isinstance(raw_turns, list) or not raw_turns:
            raise ValueError("blind case requires at least one turn")
        case = cls(
            case_id=str(payload.get("case_id") or "").strip(),
            actor_id=UUID(str(payload.get("actor_id") or "")),
            conversation_id=str(payload.get("conversation_id") or "").strip(),
            initial_state=(
                freeze_json_value(state, path="blind case initial_state")
                if state is not None
                else None
            ),
            initial_daily_snapshot=freeze_json_value(
                snapshot, path="blind case initial_daily_snapshot"
            ),
            runtime_config=freeze_json_value(runtime_config, path="blind case runtime_config"),
            turns=tuple(BlindTurnInput.from_mapping(item) for item in raw_turns),
        )
        if not case.case_id or not case.conversation_id:
            raise ValueError("blind case requires opaque case and conversation ids")
        if len({turn.turn_id for turn in case.turns}) != len(case.turns):
            raise ValueError(f"blind case {case.case_id!r} contains duplicate turn ids")
        return case

    def as_mapping(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "actor_id": str(self.actor_id),
            "conversation_id": self.conversation_id,
            "initial_state": (
                thaw_json_value(self.initial_state) if self.initial_state is not None else None
            ),
            "initial_daily_snapshot": thaw_json_value(self.initial_daily_snapshot),
            "runtime_config": thaw_json_value(self.runtime_config),
            "turns": [turn.as_mapping() for turn in self.turns],
        }


@dataclass(frozen=True)
class BlindInputPack:
    pack_id: str
    cases: tuple[BlindRuntimeCase, ...]
    digest: str
    schema_version: str = BLIND_INPUT_SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BlindInputPack":
        if not isinstance(payload, Mapping):
            raise ValueError("blind input pack must be an object")
        assert_no_oracle_fields(payload, path="blind_input")
        _require_closed_fields(payload, _PACK_FIELDS, context="blind input pack")
        if payload.get("schema_version") != BLIND_INPUT_SCHEMA_VERSION:
            raise ValueError(f"unsupported blind input schema: {payload.get('schema_version')!r}")
        raw_cases = payload.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValueError("blind input pack requires at least one case")
        cases = tuple(BlindRuntimeCase.from_mapping(item) for item in raw_cases)
        if len({case.case_id for case in cases}) != len(cases):
            raise ValueError("blind input pack contains duplicate case ids")
        pack_id = str(payload.get("pack_id") or "").strip()
        if not pack_id:
            raise ValueError("blind input pack requires pack_id")
        canonical = {
            "schema_version": BLIND_INPUT_SCHEMA_VERSION,
            "pack_id": pack_id,
            "cases": [case.as_mapping() for case in cases],
        }
        digest = _json_digest(canonical)
        supplied_digest = str(payload.get("digest") or "")
        if supplied_digest and supplied_digest != digest:
            raise ValueError("blind input pack digest is invalid")
        return cls(pack_id=pack_id, cases=cases, digest=digest)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pack_id": self.pack_id,
            "digest": self.digest,
            "cases": [case.as_mapping() for case in self.cases],
        }


@dataclass(frozen=True)
class BlindActualArtifact:
    run_id: str
    input_pack_id: str
    input_pack_digest: str
    runtime_version_hash: str
    cases: tuple[Mapping[str, Any], ...]
    artifact_hash: str
    completed: bool = True
    schema_version: str = BLIND_ACTUAL_SCHEMA_VERSION

    @classmethod
    def completed_artifact(
        cls,
        *,
        input_pack: BlindInputPack,
        runtime_version_hash: str,
        cases: tuple[Mapping[str, Any], ...],
    ) -> "BlindActualArtifact":
        run_id = hashlib.sha256(
            f"{input_pack.digest}:{runtime_version_hash}".encode("utf-8")
        ).hexdigest()[:24]
        frozen_cases = tuple(
            freeze_json_value(case, path=f"Runtime actual artifact cases[{index}]")
            for index, case in enumerate(cases)
        )
        body = {
            "schema_version": BLIND_ACTUAL_SCHEMA_VERSION,
            "run_id": run_id,
            "input_pack_id": input_pack.pack_id,
            "input_pack_digest": input_pack.digest,
            "runtime_version_hash": runtime_version_hash,
            "completed": True,
            "cases": [thaw_json_value(case) for case in frozen_cases],
        }
        return cls(
            run_id=run_id,
            input_pack_id=input_pack.pack_id,
            input_pack_digest=input_pack.digest,
            runtime_version_hash=runtime_version_hash,
            cases=frozen_cases,
            artifact_hash=_json_digest(body),
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BlindActualArtifact":
        allowed = frozenset(
            {
                "schema_version",
                "run_id",
                "input_pack_id",
                "input_pack_digest",
                "runtime_version_hash",
                "completed",
                "cases",
                "artifact_hash",
            }
        )
        _require_closed_fields(payload, allowed, context="Runtime actual artifact")
        if payload.get("schema_version") != BLIND_ACTUAL_SCHEMA_VERSION:
            raise ValueError("unsupported Runtime actual artifact schema")
        if payload.get("completed") is not True:
            raise ValueError("Runtime actual artifact is not completed")
        cases = payload.get("cases")
        if not isinstance(cases, list):
            raise ValueError("Runtime actual artifact cases must be an array")
        body = {
            key: thaw_json_value(payload[key])
            for key in allowed
            if key != "artifact_hash"
        }
        actual_hash = str(payload.get("artifact_hash") or "")
        if not actual_hash or actual_hash != _json_digest(body):
            raise ValueError("Runtime actual artifact completion hash is invalid")
        return cls(
            run_id=str(payload.get("run_id") or ""),
            input_pack_id=str(payload.get("input_pack_id") or ""),
            input_pack_digest=str(payload.get("input_pack_digest") or ""),
            runtime_version_hash=str(payload.get("runtime_version_hash") or ""),
            cases=tuple(
                freeze_json_value(case, path=f"Runtime actual artifact cases[{index}]")
                for index, case in enumerate(cases)
            ),
            artifact_hash=actual_hash,
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "input_pack_id": self.input_pack_id,
            "input_pack_digest": self.input_pack_digest,
            "runtime_version_hash": self.runtime_version_hash,
            "completed": self.completed,
            "cases": [thaw_json_value(case) for case in self.cases],
            "artifact_hash": self.artifact_hash,
        }


class FileActualArtifactStore:
    """Atomic local store for completed, hash-verified Runtime actual artifacts."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def publish(self, artifact: BlindActualArtifact) -> None:
        verified = BlindActualArtifact.from_mapping(artifact.as_mapping())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(verified.as_mapping(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._path)

    def load_completed(self) -> BlindActualArtifact:
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        return BlindActualArtifact.from_mapping(payload)


class BlindSemanticRunner:
    """Run the Runtime from blind inputs and emit a label-free actual artifact.

    Labels and scorers are absent from this interface and module dependency
    graph. The artifact is completion-hashed before a separate scorer may read
    it.
    """

    def __init__(
        self,
        interpreter: SemanticInterpreter,
        *,
        runtime_version_hash: str | None = None,
        max_concurrency: int = 1,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("Blind Runtime concurrency must be at least one")
        self._interpreter = interpreter
        self._runtime_version_hash = runtime_version_hash or _runtime_version_hash(
            runtime_identity=_interpreter_runtime_identity(interpreter)
        )
        self._max_concurrency = max_concurrency

    async def run(self, input_pack: BlindInputPack) -> BlindActualArtifact:
        input_pack = BlindInputPack.from_mapping(input_pack.as_mapping())
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def run_case(case: BlindRuntimeCase) -> Mapping[str, Any]:
            async with semaphore:
                return await self._run_case(case)

        case_artifacts = tuple(await asyncio.gather(*(run_case(case) for case in input_pack.cases)))
        return BlindActualArtifact.completed_artifact(
            input_pack=input_pack,
            runtime_version_hash=self._runtime_version_hash,
            cases=case_artifacts,
        )

    async def _run_case(self, case: BlindRuntimeCase) -> Mapping[str, Any]:
        initial_state = _initial_state(case)
        state_store = InMemoryConversationStateStore(
            (initial_state,) if initial_state is not None else ()
        )
        daily = InMemoryDailyDomainExecutor(_daily_snapshot(case))
        audit_sink = InMemoryRuntimeAuditSink()
        config = thaw_json_value(case.runtime_config)
        harness = compose_phase1_runtime(
            mode="replay",
            interpreter=self._interpreter,
            state_store=state_store,
            daily=daily,
            daily_policy=dict(config.get("daily_policy") or {}),
            active_tasks=tuple(config.get("active_tasks") or ()),
            audit_sink=audit_sink,
        )
        turns: list[dict[str, Any]] = []
        for turn in case.turns:
            request = RuntimeTurnRequest(
                tenant_id="blind-offline-tenant",
                actor=RuntimeActor(actor_id=case.actor_id),
                conversation_id=case.conversation_id,
                message_id=f"{case.case_id}:{turn.turn_id}",
                text=turn.raw_text,
                occurred_at=turn.occurred_at,
                channel=turn.channel,
                request_metadata=turn.request_metadata,
            )
            outcome = await harness.handle(request)
            turns.append(
                _actual_turn(
                    case_id=case.case_id,
                    turn=turn,
                    outcome=outcome,
                    runtime_version_hash=self._runtime_version_hash,
                )
            )
        return freeze_json_value(
            {"case_id": case.case_id, "turns": turns},
            path=f"Runtime actual case {case.case_id}",
        )


def _actual_turn(
    *,
    case_id: str,
    turn: BlindTurnInput,
    outcome: RuntimeTurnOutcome | RuntimeFailureOutcome,
    runtime_version_hash: str,
) -> dict[str, Any]:
    decision = outcome.decision.as_dict() if outcome.decision is not None else None
    plan = outcome.command_plan.as_dict() if outcome.command_plan is not None else None
    typed_commands = []
    planning_blocks = []
    if plan is not None:
        typed_commands = [
            *plan.get("daily_commands", []),
            *plan.get("business_commands", []),
        ]
        planning_blocks = list(plan.get("blocked_actions", []))
    domain_results = [
        {
            "domain_id": result.domain_id,
            "status": result.status,
            "command_count": result.command_count,
            "actual_write": result.actual_write,
            "would_write": result.would_write,
            "command_results": [dict(item) for item in result.command_results],
        }
        for result in outcome.domain_results
    ]
    trace = [
        {"sequence": event.sequence, "stage": event.stage, "detail": dict(event.detail)}
        for event in outcome.trace
    ]
    ownership: dict[str, str] = {}
    for event in outcome.trace:
        if event.stage == "domains_resolved":
            ownership = dict(event.detail.get("action_domains") or {})
            break
    state_payload = outcome.state.as_payload() if outcome.state is not None else None
    current_goal = None
    if state_payload is not None:
        current_goal = state_payload.get("current_goal")
    return {
        "case_id": case_id,
        "turn_id": turn.turn_id,
        "input_hash": _json_digest(turn.as_mapping()),
        "runtime_version_hash": runtime_version_hash,
        "goal": current_goal,
        "entities": list((decision or {}).get("entities") or []),
        "segments": list((decision or {}).get("segments") or []),
        "domain_ownership": ownership,
        "action_class": [
            action.get("action_type") for action in (decision or {}).get("required_actions") or []
        ],
        "write_intent": bool(outcome.would_write),
        "clarification_requirement": bool((decision or {}).get("clarification_need")),
        "executable_status": outcome.status,
        "typed_commands": typed_commands,
        "planning_blocks": planning_blocks,
        "outcome": {
            "status": outcome.status,
            "reply_type": outcome.reply.reply_type,
            "actual_write": outcome.actual_write,
            "would_write": outcome.would_write,
        },
        "error_code": (
            outcome.error_code if isinstance(outcome, RuntimeFailureOutcome) else None
        ),
        "failed_stage": (
            outcome.failed_stage if isinstance(outcome, RuntimeFailureOutcome) else None
        ),
        "status": outcome.status,
        "actual_write": outcome.actual_write,
        "would_write": outcome.would_write,
        "decision": decision,
        "planner_output": plan,
        "domain_results": domain_results,
        "state": state_payload,
        "trace": trace,
        "legacy_fallback_used": outcome.legacy_fallback_used,
    }


def _initial_state(case: BlindRuntimeCase) -> ConversationState | None:
    if case.initial_state is None:
        return None
    state = ConversationState.from_payload(thaw_json_value(case.initial_state))
    if state.user_id != str(case.actor_id) or state.conversation_id != case.conversation_id:
        raise ValueError(f"blind case {case.case_id!r} initial state identity mismatch")
    return state


def _daily_snapshot(case: BlindRuntimeCase) -> DailyReportMutationSnapshot:
    payload = thaw_json_value(case.initial_daily_snapshot)
    item_ids_raw = payload.get("item_ids") or {}
    if not isinstance(item_ids_raw, Mapping):
        raise ValueError(f"blind case {case.case_id!r} item_ids must be an object")
    snapshot = DailyReportMutationSnapshot(
        report_id=UUID(str(payload.get("report_id") or "")),
        owner_user_id=case.actor_id,
        version=int(payload.get("version", 0)),
        status=str(payload.get("status") or "collecting"),
        today_work=tuple(str(value) for value in payload.get("today_work") or ()),
        problems=tuple(str(value) for value in payload.get("problems") or ()),
        tomorrow_plan=tuple(str(value) for value in payload.get("tomorrow_plan") or ()),
        item_ids={
            str(field_name): tuple(str(value) for value in values)
            for field_name, values in item_ids_raw.items()
        },
    )
    for field_name in ("today_work", "problems", "tomorrow_plan"):
        values = tuple(getattr(snapshot, field_name))
        ids = tuple(snapshot.item_ids.get(field_name, ()))
        if ids and len(ids) != len(values):
            raise ValueError(
                f"blind case {case.case_id!r} snapshot item IDs do not match {field_name}"
            )
    return snapshot


def _runtime_version_material_paths() -> tuple[Path, ...]:
    """Return every local source artifact that can affect Blind Runtime output."""

    runtime_dir = Path(__file__).resolve().parent
    agent2_dir = runtime_dir.parent
    app_dir = agent2_dir.parent
    runtime_materials = {
        runtime_dir / name
        for name in (
            "__init__.py",
            "blind.py",
            "composition.py",
            "context.py",
            "contracts.py",
            "domains.py",
            "harness.py",
        )
    }
    paths = {
        agent2_dir / "cognitive_contract_v3.py",
        agent2_dir / "semantic_interpreter_v3.py",
        agent2_dir / "cognitive_core_v3.py",
        agent2_dir / "command_planner_v3.py",
        agent2_dir / "conversation_state.py",
        agent2_dir / "conversation_state_store.py",
        agent2_dir / "oracle_guard.py",
        agent2_dir / "json_immutability.py",
        agent2_dir / "typed_daily_commands.py",
        app_dir / "llm" / "client.py",
        app_dir / "llm" / "prompts" / "cognitive_core_v3.md",
        app_dir / "utils" / "json.py",
        *runtime_materials,
    }
    return tuple(sorted(paths, key=lambda path: path.as_posix()))


def _runtime_version_hash(
    *,
    paths: tuple[Path, ...] | None = None,
    runtime_identity: Mapping[str, Any] | None = None,
) -> str:
    repo_root = Path(__file__).resolve().parents[3]
    digest = hashlib.sha256()
    for path in paths or _runtime_version_material_paths():
        try:
            label = path.resolve().relative_to(repo_root).as_posix()
        except ValueError:
            label = path.name
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    digest.update(b"runtime_identity\0")
    digest.update(
        json.dumps(
            dict(runtime_identity or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return digest.hexdigest()


def _interpreter_runtime_identity(interpreter: SemanticInterpreter) -> Mapping[str, Any]:
    identity_provider = getattr(interpreter, "runtime_identity", None)
    if callable(identity_provider):
        identity = identity_provider()
        if not isinstance(identity, Mapping):
            raise TypeError("semantic interpreter runtime identity must be a mapping")
        return dict(identity)
    return {"adapter": f"{type(interpreter).__module__}.{type(interpreter).__qualname__}"}


def _require_closed_fields(
    payload: Mapping[str, Any],
    allowed: frozenset[str],
    *,
    context: str,
) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"{context} contains unknown fields: {sorted(unknown)}")


def _datetime(value: Any, *, context: str) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise ValueError(f"{context} must be an ISO timestamp") from exc
    if result.tzinfo is None:
        raise ValueError(f"{context} must be timezone-aware")
    return result


def _json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
