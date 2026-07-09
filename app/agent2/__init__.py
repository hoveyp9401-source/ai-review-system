"""Agent 2.0 support modules.

The package exports common helpers lazily so lower-level modules can import
`app.agent2.daily_state` without pulling the whole Agent2 graph back in.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "CoordinationAction": ("app.agent2.coordination_plan", "CoordinationAction"),
    "CoordinationPlan": ("app.agent2.coordination_plan", "CoordinationPlan"),
    "CoordinationSandboxResult": ("app.agent2.coordination_sandbox", "CoordinationSandboxResult"),
    "Agent2ContextPack": ("app.agent2.context_pack", "Agent2ContextPack"),
    "ChatCapabilityReply": ("app.agent2.chat_capability", "ChatCapabilityReply"),
    "DailyCommand": ("app.agent2.daily_commands", "DailyCommand"),
    "DailyShadowEvaluation": ("app.agent2.daily_shadow", "DailyShadowEvaluation"),
    "DialogueCase": ("app.agent2.dialogue_replay", "DialogueCase"),
    "CaseTableDocument": ("app.agent2.case_table_rag", "CaseTableDocument"),
    "CaseTableRagAdapter": ("app.agent2.case_table_rag", "CaseTableRagAdapter"),
    "CaseRegistryRecord": ("app.agent2.knowledge_resolver", "CaseRegistryRecord"),
    "DailyReportHistoryRecord": ("app.agent2.knowledge_resolver", "DailyReportHistoryRecord"),
    "InMemoryDailyReportHistoryAdapter": ("app.agent2.knowledge_resolver", "InMemoryDailyReportHistoryAdapter"),
    "InMemoryCaseRegistryAdapter": ("app.agent2.knowledge_resolver", "InMemoryCaseRegistryAdapter"),
    "InMemoryOrgDirectoryAdapter": ("app.agent2.knowledge_resolver", "InMemoryOrgDirectoryAdapter"),
    "KnowledgeEvidenceFrame": ("app.agent2.context_pack", "KnowledgeEvidenceFrame"),
    "KnowledgeQuery": ("app.agent2.knowledge_resolver", "KnowledgeQuery"),
    "KnowledgeResolution": ("app.agent2.knowledge_resolver", "KnowledgeResolution"),
    "OrgTeamRecord": ("app.agent2.knowledge_resolver", "OrgTeamRecord"),
    "OrgUserRecord": ("app.agent2.knowledge_resolver", "OrgUserRecord"),
    "PersonalMemoryProfile": ("app.agent2.personal_memory", "PersonalMemoryProfile"),
    "load_live_daily_history_adapter": ("app.agent2.knowledge_resolver", "load_live_daily_history_adapter"),
    "load_live_org_directory_adapter": ("app.agent2.knowledge_resolver", "load_live_org_directory_adapter"),
    "LegacyDailyAdapter": ("app.agent2.legacy_daily_adapter", "LegacyDailyAdapter"),
    "LegacyDailyAdapterResult": ("app.agent2.legacy_daily_adapter", "LegacyDailyAdapterResult"),
    "SandboxCandidate": ("app.agent2.coordination_sandbox", "SandboxCandidate"),
    "ShadowReplayRecord": ("app.agent2.shadow_replay", "ShadowReplayRecord"),
    "build_agent2_context_pack": ("app.agent2.context_pack", "build_agent2_context_pack"),
    "build_chat_reply": ("app.agent2.chat_capability", "build_chat_reply"),
    "search_case_table_index": ("app.agent2.case_table_rag", "search_case_table_index"),
    "write_case_table_index": ("app.agent2.case_table_rag", "write_case_table_index"),
    "build_personal_memory_profile": ("app.agent2.personal_memory", "build_personal_memory_profile"),
    "compile_coordination_plan": ("app.agent2.coordination_plan", "compile_coordination_plan"),
    "compile_daily_commands": ("app.agent2.daily_commands", "compile_daily_commands"),
    "build_coordination_sandbox": ("app.agent2.coordination_sandbox", "build_coordination_sandbox"),
    "evaluate_daily_shadow": ("app.agent2.daily_shadow", "evaluate_daily_shadow"),
    "resolve_knowledge": ("app.agent2.knowledge_resolver", "resolve_knowledge"),
    "replay_dialogue_cases": ("app.agent2.dialogue_replay", "replay_dialogue_cases"),
    "replay_shadow_records": ("app.agent2.shadow_replay", "replay_shadow_records"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
