from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from app.agent2.cognitive_core_v3 import (
    CognitiveTurn,
    SemanticInterpretation,
    SemanticInterpreter,
)
from app.agent2.conversation_state import ConversationState
from app.agent2.domain_admission import DomainAdmissionEngine
from app.agent2.json_immutability import freeze_json_value, thaw_json_value
from app.agent2.oracle_guard import assert_no_oracle_fields


BLIND_INPUT_SCHEMA_VERSION = "agent2.semantic_admission_blind_input.v1"
ACTUAL_ARTIFACT_SCHEMA_VERSION = "agent2.semantic_admission_actual.v1"

_PACK_FIELDS = frozenset({"schema_version", "pack_id", "cases", "digest"})
_CASE_FIELDS = frozenset({"case_id", "scope", "raw_text", "state", "resources"})
_SCOPE_FIELDS = frozenset(
    {
        "tenant_id",
        "user_id",
        "actor_user_id",
        "conversation_id",
        "message_id",
        "occurred_at",
        "channel",
    }
)
_EXTRA_ORACLE_KEYS = frozenset({"oracle", "oracle_label", "oracle_decision"})


@dataclass(frozen=True)
class SemanticAdmissionBlindScope:
    tenant_id: str
    user_id: str
    actor_user_id: str
    conversation_id: str
    message_id: str
    occurred_at: datetime
    channel: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SemanticAdmissionBlindScope":
        _require_closed_fields(payload, _SCOPE_FIELDS, context="blind admission scope")
        occurred_at = _aware_datetime(payload.get("occurred_at"), context="occurred_at")
        scope = cls(
            tenant_id=str(payload.get("tenant_id") or "").strip(),
            user_id=str(payload.get("user_id") or "").strip(),
            actor_user_id=str(payload.get("actor_user_id") or "").strip(),
            conversation_id=str(payload.get("conversation_id") or "").strip(),
            message_id=str(payload.get("message_id") or "").strip(),
            occurred_at=occurred_at,
            channel=str(payload.get("channel") or "").strip(),
        )
        if not all(
            (
                scope.tenant_id,
                scope.user_id,
                scope.actor_user_id,
                scope.conversation_id,
                scope.message_id,
                scope.channel,
            )
        ):
            raise ValueError("blind admission scope requires verified identity and message fields")
        return scope

    def as_mapping(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "actor_user_id": self.actor_user_id,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "occurred_at": self.occurred_at.isoformat(),
            "channel": self.channel,
        }


@dataclass(frozen=True)
class SemanticAdmissionBlindCase:
    case_id: str
    scope: SemanticAdmissionBlindScope
    raw_text: str
    state: Mapping[str, Any] | None
    resources: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SemanticAdmissionBlindCase":
        _require_closed_fields(payload, _CASE_FIELDS, context="blind admission case")
        scope_raw = payload.get("scope")
        state_raw = payload.get("state")
        resources_raw = payload.get("resources")
        if not isinstance(scope_raw, Mapping):
            raise ValueError("blind admission case scope must be an object")
        if state_raw is not None and not isinstance(state_raw, Mapping):
            raise ValueError("blind admission case state must be an object or null")
        if not isinstance(resources_raw, Mapping):
            raise ValueError("blind admission case resources must be an object")
        scope = SemanticAdmissionBlindScope.from_mapping(scope_raw)
        case = cls(
            case_id=str(payload.get("case_id") or "").strip(),
            scope=scope,
            raw_text=str(payload.get("raw_text") or ""),
            state=(
                freeze_json_value(state_raw, path="blind admission case state")
                if state_raw is not None
                else None
            ),
            resources=freeze_json_value(
                resources_raw, path="blind admission case resources"
            ),
        )
        if not case.case_id or not case.raw_text.strip():
            raise ValueError("blind admission case requires case_id and raw_text")
        state = case.conversation_state()
        if (
            state.user_id != scope.user_id
            or state.conversation_id != scope.conversation_id
        ):
            raise ValueError("blind admission case state identity does not match scope")
        return case

    def conversation_state(self) -> ConversationState:
        if self.state is None:
            return ConversationState.empty(
                user_id=self.scope.user_id,
                conversation_id=self.scope.conversation_id,
            )
        return ConversationState.from_payload(thaw_json_value(self.state))

    def as_mapping(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "scope": self.scope.as_mapping(),
            "raw_text": self.raw_text,
            "state": thaw_json_value(self.state) if self.state is not None else None,
            "resources": thaw_json_value(self.resources),
        }


@dataclass(frozen=True)
class SemanticAdmissionBlindPack:
    pack_id: str
    cases: tuple[SemanticAdmissionBlindCase, ...]
    digest: str
    schema_version: str = BLIND_INPUT_SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SemanticAdmissionBlindPack":
        if not isinstance(payload, Mapping):
            raise ValueError("blind admission input must be an object")
        _assert_no_oracle_fields(payload, path="semantic_admission_blind_input")
        _require_closed_fields(payload, _PACK_FIELDS, context="blind admission pack")
        if payload.get("schema_version") != BLIND_INPUT_SCHEMA_VERSION:
            raise ValueError("unsupported blind admission input schema")
        rows = payload.get("cases")
        if not isinstance(rows, list) or not rows:
            raise ValueError("blind admission pack requires at least one case")
        cases = tuple(SemanticAdmissionBlindCase.from_mapping(row) for row in rows)
        if len({case.case_id for case in cases}) != len(cases):
            raise ValueError("blind admission pack contains duplicate case ids")
        if len({case.scope.message_id for case in cases}) != len(cases):
            raise ValueError("blind admission pack contains duplicate message ids")
        pack_id = str(payload.get("pack_id") or "").strip()
        if not pack_id:
            raise ValueError("blind admission pack requires pack_id")
        canonical = {
            "schema_version": BLIND_INPUT_SCHEMA_VERSION,
            "pack_id": pack_id,
            "cases": [case.as_mapping() for case in cases],
        }
        digest = _json_digest(canonical)
        supplied_digest = str(payload.get("digest") or "")
        if supplied_digest and supplied_digest != digest:
            raise ValueError("blind admission pack digest is invalid")
        return cls(pack_id=pack_id, cases=cases, digest=digest)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pack_id": self.pack_id,
            "cases": [case.as_mapping() for case in self.cases],
            "digest": self.digest,
        }


@dataclass(frozen=True)
class SemanticAdmissionActualArtifact:
    run_id: str
    input_pack_id: str
    input_pack_digest: str
    runtime_version_hash: str
    cases: tuple[Mapping[str, Any], ...]
    artifact_hash: str
    completed: bool = True
    schema_version: str = ACTUAL_ARTIFACT_SCHEMA_VERSION

    @classmethod
    def completed_artifact(
        cls,
        *,
        input_pack: SemanticAdmissionBlindPack,
        runtime_version_hash: str,
        cases: tuple[Mapping[str, Any], ...],
    ) -> "SemanticAdmissionActualArtifact":
        frozen_cases = tuple(
            freeze_json_value(row, path=f"semantic admission actual cases[{index}]")
            for index, row in enumerate(cases)
        )
        run_id = hashlib.sha256(
            f"{input_pack.digest}:{runtime_version_hash}".encode("utf-8")
        ).hexdigest()[:24]
        body = {
            "schema_version": ACTUAL_ARTIFACT_SCHEMA_VERSION,
            "run_id": run_id,
            "input_pack_id": input_pack.pack_id,
            "input_pack_digest": input_pack.digest,
            "runtime_version_hash": runtime_version_hash,
            "completed": True,
            "evidence_classification": "machine_candidate",
            "human_review_state": "pending",
            "cases": [thaw_json_value(row) for row in frozen_cases],
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
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SemanticAdmissionActualArtifact":
        allowed = frozenset(
            {
                "schema_version",
                "run_id",
                "input_pack_id",
                "input_pack_digest",
                "runtime_version_hash",
                "completed",
                "evidence_classification",
                "human_review_state",
                "cases",
                "artifact_hash",
            }
        )
        _require_closed_fields(payload, allowed, context="semantic admission actual artifact")
        if payload.get("schema_version") != ACTUAL_ARTIFACT_SCHEMA_VERSION:
            raise ValueError("unsupported semantic admission actual artifact schema")
        if payload.get("completed") is not True:
            raise ValueError("semantic admission actual artifact is not completed")
        if payload.get("evidence_classification") != "machine_candidate":
            raise ValueError("semantic admission actual artifact must remain a machine candidate")
        if payload.get("human_review_state") != "pending":
            raise ValueError("semantic admission actual artifact must remain pending human review")
        rows = payload.get("cases")
        if not isinstance(rows, list):
            raise ValueError("semantic admission actual artifact cases must be an array")
        body = {key: thaw_json_value(payload[key]) for key in allowed if key != "artifact_hash"}
        artifact_hash = str(payload.get("artifact_hash") or "")
        if not artifact_hash or artifact_hash != _json_digest(body):
            raise ValueError("semantic admission actual artifact hash is invalid")
        return cls(
            run_id=str(payload.get("run_id") or ""),
            input_pack_id=str(payload.get("input_pack_id") or ""),
            input_pack_digest=str(payload.get("input_pack_digest") or ""),
            runtime_version_hash=str(payload.get("runtime_version_hash") or ""),
            cases=tuple(
                freeze_json_value(row, path=f"semantic admission actual cases[{index}]")
                for index, row in enumerate(rows)
            ),
            artifact_hash=artifact_hash,
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "input_pack_id": self.input_pack_id,
            "input_pack_digest": self.input_pack_digest,
            "runtime_version_hash": self.runtime_version_hash,
            "completed": self.completed,
            "evidence_classification": "machine_candidate",
            "human_review_state": "pending",
            "cases": [thaw_json_value(row) for row in self.cases],
            "artifact_hash": self.artifact_hash,
        }


class SemanticAdmissionBlindRunner:
    """Run interpreter -> Domain Admission without loading labels or a scorer."""

    def __init__(
        self,
        interpreter: SemanticInterpreter,
        *,
        admission_engine: DomainAdmissionEngine | None = None,
        runtime_version_hash: str | None = None,
        max_concurrency: int = 1,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("semantic admission blind concurrency must be positive")
        self._interpreter = interpreter
        self._admission_engine = admission_engine or DomainAdmissionEngine()
        self._runtime_version_hash_override = str(runtime_version_hash or "").strip()
        self._max_concurrency = int(max_concurrency)

    async def run(
        self, pack: SemanticAdmissionBlindPack
    ) -> SemanticAdmissionActualArtifact:
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def run_one(case: SemanticAdmissionBlindCase) -> Mapping[str, Any]:
            async with semaphore:
                return await self._run_case(case)

        rows = tuple(await asyncio.gather(*(run_one(case) for case in pack.cases)))
        runtime_version_hash = self._runtime_version_hash_override or _runtime_version_hash(
            _interpreter_runtime_identity(self._interpreter)
        )
        return SemanticAdmissionActualArtifact.completed_artifact(
            input_pack=pack,
            runtime_version_hash=runtime_version_hash,
            cases=rows,
        )

    async def _run_case(self, case: SemanticAdmissionBlindCase) -> Mapping[str, Any]:
        state = case.conversation_state()
        turn = CognitiveTurn(
            tenant_id=case.scope.tenant_id,
            actor_user_id=case.scope.actor_user_id,
            user_id=case.scope.user_id,
            conversation_id=case.scope.conversation_id,
            message_id=case.scope.message_id,
            text=case.raw_text,
            occurred_at=case.scope.occurred_at,
            resources=thaw_json_value(case.resources),
        )
        proposal = await self._interpreter.interpret(turn, state)
        if not isinstance(proposal, SemanticInterpretation):
            raise TypeError("semantic interpreter must return SemanticInterpretation")
        admission = self._admission_engine.admit(turn, state, proposal)
        selection_requests = getattr(admission, "selection_requests", ()) or ()
        return {
            "case_id": case.case_id,
            "source_message_sha256": hashlib.sha256(
                case.raw_text.encode("utf-8")
            ).hexdigest(),
            "proposal": _interpretation_mapping(proposal),
            "admitted_interpretation": _interpretation_mapping(admission.interpretation),
            "decisions": [item.as_dict() for item in admission.decisions],
            "tickets": [item.as_dict() for item in admission.tickets],
            "information_pendings": [
                item.as_dict() for item in admission.information_pendings
            ],
            "selection_requests": [
                item.as_dict() if hasattr(item, "as_dict") else thaw_json_value(item)
                for item in selection_requests
            ],
            "trace": admission.trace.as_dict(),
            "evidence_classification": "machine_candidate",
            "human_review_state": "pending",
        }


class SemanticAdmissionActualArtifactStore:
    """Atomically publish only completed, hash-verified actual artifacts."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def publish(self, artifact: SemanticAdmissionActualArtifact) -> None:
        verified = SemanticAdmissionActualArtifact.from_mapping(artifact.as_mapping())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(verified.as_mapping(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._path)

    def load_completed(self) -> SemanticAdmissionActualArtifact:
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        return SemanticAdmissionActualArtifact.from_mapping(payload)


def _interpretation_mapping(value: SemanticInterpretation) -> dict[str, Any]:
    return {
        "intents": list(value.intents),
        "segments": [
            {
                "segment_id": item.segment_id,
                "text": item.text,
                "text_hash": item.text_hash,
                "intents": list(item.intents),
                "entity_ids": list(item.entity_ids),
                "action_ids": list(item.action_ids),
                "start_offset": item.start_offset,
                "end_offset": item.end_offset,
            }
            for item in value.segments
        ],
        "entities": [
            {
                "entity_id": item.entity_id,
                "entity_type": item.entity_type,
                "value": item.value,
                "confidence": item.confidence,
                "attributes": dict(item.attributes),
                "source_context_id": item.source_context_id,
            }
            for item in value.entities
        ],
        "confidence": value.confidence,
        "required_actions": [
            {
                "action_id": item.action_id,
                "action_type": item.action_type,
                "intent": item.intent,
                "entity_ids": list(item.entity_ids),
                "parameters": dict(item.parameters),
            }
            for item in value.required_actions
        ],
        "clarification_need": (
            {
                "reason": value.clarification_need.reason,
                "missing_fields": list(value.clarification_need.missing_fields),
                "question": value.clarification_need.question,
            }
            if value.clarification_need is not None
            else None
        ),
        "context_update": {
            "current_goal": value.context_update.current_goal,
            "preserve_current_goal": value.context_update.preserve_current_goal,
            "remember_entity_ids": list(value.context_update.remember_entity_ids),
            "remember_turn": value.context_update.remember_turn,
            "resume_previous_goal": value.context_update.resume_previous_goal,
            "clear_current_goal": value.context_update.clear_current_goal,
            "consumed_pending_ids": list(value.context_update.consumed_pending_ids),
        },
    }


def _interpreter_runtime_identity(interpreter: SemanticInterpreter) -> Mapping[str, Any]:
    provider = getattr(interpreter, "runtime_identity", None)
    if callable(provider):
        value = provider()
        if not isinstance(value, Mapping):
            raise TypeError("semantic interpreter runtime identity must be a mapping")
        return dict(value)
    return {"adapter": f"{type(interpreter).__module__}.{type(interpreter).__qualname__}"}


def _runtime_version_hash(interpreter_identity: Mapping[str, Any]) -> str:
    root = Path(__file__).resolve().parents[3]
    paths = (
        Path(__file__).resolve(),
        root / "app" / "agent2" / "cognitive_core_v3.py",
        root / "app" / "agent2" / "semantic_interpreter_v3.py",
        root / "app" / "agent2" / "domain_admission.py",
        root / "app" / "agent2" / "admission_contracts.py",
        root / "app" / "llm" / "prompts" / "cognitive_core_v3.md",
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    digest.update(b"interpreter_identity\0")
    digest.update(
        json.dumps(
            dict(interpreter_identity),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return digest.hexdigest()


def _assert_no_oracle_fields(value: Any, *, path: str) -> None:
    assert_no_oracle_fields(value, path=path)
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _EXTRA_ORACLE_KEYS or normalized.startswith("oracle_"):
                raise ValueError(f"forbidden oracle field at {path}.{key}")
            _assert_no_oracle_fields(nested, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _assert_no_oracle_fields(nested, path=f"{path}[{index}]")


def _require_closed_fields(
    payload: Mapping[str, Any], allowed: frozenset[str], *, context: str
) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"{context} contains unknown fields: {sorted(unknown)}")


def _aware_datetime(value: Any, *, context: str) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise ValueError(f"{context} must be an ISO timestamp") from exc
    if result.tzinfo is None:
        raise ValueError(f"{context} must be timezone-aware")
    return result


def _json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
