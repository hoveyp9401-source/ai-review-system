from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Iterable


def load_execution_turns(path: str | Path) -> list[dict[str, Any]]:
    """Load Agent2 replay/audit JSONL and flatten it to turn records."""

    source = Path(path)
    turns: list[dict[str, Any]] = []
    if not source.exists():
        return turns
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        turns.extend(_turns_from_record(record))
    return turns


def render_audit_viewer_html(
    turns: Iterable[dict[str, Any]],
    output_path: str | Path,
    *,
    title: str = "Agent2 Cognitive Audit Viewer",
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = list(turns)
    output.write_text(_html_document(rows, title=title), encoding="utf-8")
    return output


def _turns_from_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(record.get("turns"), list):
        dialogue_id = str(record.get("dialogue_id") or "")
        result: list[dict[str, Any]] = []
        for turn in record.get("turns") or []:
            if isinstance(turn, dict):
                payload = dict(turn)
                payload.setdefault("dialogue_id", dialogue_id)
                result.append(payload)
        return result
    if "text" in record or "cognitive_decision" in record:
        return [record]
    return []


def _html_document(turns: list[dict[str, Any]], *, title: str) -> str:
    total = len(turns)
    writes = sum(1 for turn in turns if _bool(turn.get("direct_write")) or _actual_write(turn))
    violations = sum(len(_violations(turn)) for turn in turns)
    cards = "\n".join(_turn_card(turn) for turn in turns)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_e(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7fb;
      --panel: #ffffff;
      --ink: #18202b;
      --muted: #657184;
      --line: #dfe5ef;
      --blue: #0f4c81;
      --red: #b3261e;
      --yellow: #8a5a00;
      --green: #137333;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 10;
      background: rgba(246, 247, 251, .94);
      backdrop-filter: blur(10px);
      border-bottom: 1px solid var(--line);
      padding: 18px 28px 14px;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 22px;
      letter-spacing: 0;
    }}
    .stats, .filters {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }}
    .pill {{
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 999px;
      padding: 4px 10px;
      color: var(--muted);
      font-size: 12px;
    }}
    input {{
      min-width: 300px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      font: inherit;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 18px 22px 40px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      margin: 12px 0;
      padding: 14px 16px;
      box-shadow: 0 1px 2px rgba(18, 32, 50, .04);
    }}
    .card[data-has-violations="true"] {{
      border-color: rgba(179, 38, 30, .42);
    }}
    .topline {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-bottom: 8px;
    }}
    .tag {{
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      background: #eef3f9;
      color: var(--blue);
    }}
    .tag.write {{ background: #e6f4ea; color: var(--green); }}
    .tag.block {{ background: #fff3d8; color: var(--yellow); }}
    .tag.error {{ background: #fce8e6; color: var(--red); }}
    .text {{
      font-size: 16px;
      margin: 8px 0 12px;
      white-space: pre-wrap;
    }}
    details {{
      border-top: 1px solid var(--line);
      padding-top: 8px;
      margin-top: 8px;
    }}
    summary {{
      cursor: pointer;
      color: var(--blue);
      font-weight: 600;
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #f8fafc;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      overflow: auto;
    }}
  </style>
</head>
<body>
  <header>
    <h1>{_e(title)}</h1>
    <div class="stats">
      <span class="pill">turns: {total}</span>
      <span class="pill">writes: {writes}</span>
      <span class="pill">contract violations: {violations}</span>
    </div>
    <div class="filters" style="margin-top: 10px">
      <input id="q" placeholder="Filter by text, workflow, command, violation..." oninput="filterCards()">
      <span class="pill">Open cards to inspect cognitive decision, commands, and fact contracts.</span>
    </div>
  </header>
  <main id="cards">
    {cards}
  </main>
  <script>
    function filterCards() {{
      const q = document.getElementById('q').value.toLowerCase();
      for (const card of document.querySelectorAll('.card')) {{
        card.style.display = card.dataset.search.includes(q) ? '' : 'none';
      }}
    }}
  </script>
</body>
</html>
"""


def _turn_card(turn: dict[str, Any]) -> str:
    decision = turn.get("cognitive_decision") if isinstance(turn.get("cognitive_decision"), dict) else {}
    workflow = str(turn.get("primary_workflow") or decision.get("primary_workflow") or "")
    status = str(turn.get("execution_status") or (decision.get("execution_trace") or {}).get("execution_status") or "")
    write = _bool(turn.get("direct_write")) or _actual_write(turn)
    violations = _violations(turn)
    commands = turn.get("daily_commands") or decision.get("daily_commands") or []
    facts = _fact_contracts(turn)
    action_tags = ", ".join(
        _compact_action(action)
        for action in decision.get("actions") or []
        if isinstance(action, dict)
    )
    command_tags = ", ".join(
        _compact_command(command)
        for command in commands
        if isinstance(command, dict)
    )
    tags = [
        _tag(workflow or "unknown"),
        _tag(status or "not_executed", "write" if write else ""),
        _tag("write" if write else "no-write", "write" if write else "block"),
    ]
    if violations:
        tags.append(_tag(f"violations {len(violations)}", "error"))
    search = " ".join(
        [
            str(turn.get("dialogue_id") or ""),
            str(turn.get("turn_id") or ""),
            str(turn.get("text") or ""),
            workflow,
            status,
            action_tags,
            command_tags,
            json.dumps(violations, ensure_ascii=False),
            json.dumps(facts, ensure_ascii=False),
        ]
    ).lower()
    return f"""<section class="card" data-has-violations="{str(bool(violations)).lower()}" data-search="{_attr(search)}">
  <div class="topline">
    <span class="pill">{_e(str(turn.get("dialogue_id") or ""))} / {_e(str(turn.get("turn_id") or turn.get("turn_index") or ""))}</span>
    {''.join(tags)}
  </div>
  <div class="text">{_e(str(turn.get("text") or ""))}</div>
  <div class="pill">actions: {_e(action_tags or "-")}</div>
  <div class="pill" style="margin-top: 6px">commands: {_e(command_tags or "-")}</div>
  <details>
    <summary>Audit payload</summary>
    <pre>{_json({"cognitive_decision": decision, "commands": commands, "violations": violations, "fact_contracts": facts})}</pre>
  </details>
</section>"""


def _compact_action(action: dict[str, Any]) -> str:
    return "/".join(
        part
        for part in [
            str(action.get("workflow") or ""),
            str(action.get("action_type") or ""),
            str(action.get("operation") or ""),
            str(action.get("target_field") or ""),
            str(action.get("write_policy") or ""),
        ]
        if part
    )


def _compact_command(command: dict[str, Any]) -> str:
    operation = str(command.get("operation") or "")
    target_field = str(command.get("target_field") or "")
    should_write = "write" if command.get("should_write") else "dry"
    flags = ",".join(str(flag) for flag in command.get("safety_flags") or [])
    return "/".join(part for part in [operation, target_field, should_write, flags] if part)


def _fact_contracts(turn: dict[str, Any]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for item in turn.get("knowledge_facts") or []:
        if isinstance(item, dict) and isinstance(item.get("fact_contract"), dict):
            facts.append(dict(item.get("fact_contract") or {}))
    if isinstance(turn.get("fact_contract"), dict):
        facts.append(dict(turn.get("fact_contract") or {}))
    return facts


def _violations(turn: dict[str, Any]) -> list[dict[str, Any]]:
    raw = turn.get("contract_invariant_violations") or []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _actual_write(turn: dict[str, Any]) -> bool:
    decision = turn.get("cognitive_decision") if isinstance(turn.get("cognitive_decision"), dict) else {}
    trace = decision.get("execution_trace") if isinstance(decision.get("execution_trace"), dict) else {}
    return _bool(trace.get("actual_write"))


def _bool(value: Any) -> bool:
    return bool(value is True or str(value).lower() == "true")


def _tag(text: str, cls: str = "") -> str:
    class_attr = f" {cls}" if cls else ""
    return f'<span class="tag{class_attr}">{_e(text)}</span>'


def _json(value: Any) -> str:
    return _e(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _e(value: str) -> str:
    return html.escape(value, quote=False)


def _attr(value: str) -> str:
    return html.escape(value, quote=True)
