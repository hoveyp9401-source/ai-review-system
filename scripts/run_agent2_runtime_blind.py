from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.runtime.blind import (  # noqa: E402
    BlindInputPack,
    BlindSemanticRunner,
    FileActualArtifactStore,
)
from app.agent2.semantic_interpreter_v3 import LLMCognitiveSemanticInterpreter  # noqa: E402
from app.config import Settings  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.blind_input).read_text(encoding="utf-8"))
    pack = BlindInputPack.from_mapping(payload)
    if args.limit > 0:
        pack = BlindInputPack.from_mapping(
            {
                "schema_version": pack.schema_version,
                "pack_id": f"{pack.pack_id}-limit-{args.limit}",
                "cases": [case.as_mapping() for case in pack.cases[: args.limit]],
            }
        )
    settings = Settings()
    api_key = os.environ.get(args.api_key_env, "") or settings.llm_api_key
    if not api_key:
        raise RuntimeError(
            f"blind Runtime requires an LLM credential in {args.api_key_env}; no key is persisted"
        )
    settings = Settings(
        llm_api_key=api_key,
        llm_model=args.model or settings.agent2_cognitive_core_v3_model,
        llm_timeout_seconds=args.timeout_seconds,
        llm_max_retries=args.max_retries,
    )
    client = LLMClient(settings)
    try:
        interpreter = LLMCognitiveSemanticInterpreter(
            client,
            model=args.model or settings.agent2_cognitive_core_v3_model,
            thinking_enabled=args.thinking,
        )
        artifact = await BlindSemanticRunner(
            interpreter,
            max_concurrency=args.concurrency,
        ).run(pack)
    finally:
        await client.close()
    FileActualArtifactStore(args.actual_output).publish(artifact)
    print(
        json.dumps(
            {
                "run_id": artifact.run_id,
                "actual_artifact_hash": artifact.artifact_hash,
                "cases": len(artifact.cases),
                "turns": sum(len(case.get("turns") or []) for case in artifact.cases),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Agent2 Runtime using a blind input pack. This process cannot read labels."
    )
    parser.add_argument("--blind-input", required=True)
    parser.add_argument("--actual-output", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--model", default="")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--thinking", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
