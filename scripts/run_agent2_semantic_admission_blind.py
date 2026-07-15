from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.evaluation.semantic_admission_blind import (  # noqa: E402
    SemanticAdmissionActualArtifactStore,
    SemanticAdmissionBlindPack,
    SemanticAdmissionBlindRunner,
)
from app.agent2.semantic_interpreter_v3 import (  # noqa: E402
    LLMCognitiveSemanticInterpreter,
)
from app.config import Settings  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.blind_input).read_text(encoding="utf-8"))
    pack = SemanticAdmissionBlindPack.from_mapping(payload)
    base_settings = Settings()
    api_key = os.environ.get(args.api_key_env, "") or base_settings.llm_api_key
    if not api_key:
        raise RuntimeError(
            f"semantic admission blind replay requires an LLM credential in {args.api_key_env}; no key is persisted"
        )
    model = args.model or base_settings.agent2_cognitive_core_v3_model
    settings = Settings(
        llm_api_key=api_key,
        llm_model=model,
        llm_timeout_seconds=args.timeout_seconds,
        llm_max_retries=args.max_retries,
    )
    client = LLMClient(settings)
    try:
        interpreter = LLMCognitiveSemanticInterpreter(
            client,
            model=model,
            thinking_enabled=args.thinking,
        )
        actual = await SemanticAdmissionBlindRunner(
            interpreter,
            max_concurrency=args.concurrency,
        ).run(pack)
    finally:
        await client.close()
    SemanticAdmissionActualArtifactStore(args.actual_output).publish(actual)
    print(
        json.dumps(
            {
                "run_id": actual.run_id,
                "actual_artifact_hash": actual.artifact_hash,
                "case_count": len(actual.cases),
                "evidence_classification": "machine_candidate",
                "human_review_state": "pending",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the label-free Agent2 Semantic Admission blind pack. This process has no label interface."
    )
    parser.add_argument("--blind-input", required=True)
    parser.add_argument("--actual-output", required=True)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--model", default="")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--thinking", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

