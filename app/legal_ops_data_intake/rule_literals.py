from __future__ import annotations

import ast
import hashlib
import json
import keyword
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class RuleLiteralError(ValueError):
    pass


@dataclass(frozen=True)
class SafeRuleLiteralCatalog:
    mappings: dict[str, dict[str, str]]
    collections: dict[str, tuple[str, ...]]

    def display_items(self) -> tuple[dict[str, Any], ...]:
        items: list[dict[str, Any]] = []
        for name, mapping in sorted(self.mappings.items()):
            items.append(
                {
                    "name": name,
                    "kind": "mapping",
                    "kind_label": "精确映射",
                    "item_count": len(mapping),
                    "content_hash": _stable_hash(mapping),
                }
            )
        for name, values in sorted(self.collections.items()):
            items.append(
                {
                    "name": name,
                    "kind": "collection",
                    "kind_label": "固定值清单",
                    "item_count": len(values),
                    "content_hash": _stable_hash(values),
                }
            )
        return tuple(items)

    def prompt_summary(self) -> str:
        items: list[str] = []
        for name, mapping in sorted(self.mappings.items()):
            items.append(
                f"- {name}：精确映射，共{len(mapping)}条，"
                f"哈希{_stable_hash(mapping)[:16]}"
            )
        for name, values in sorted(self.collections.items()):
            items.append(
                f"- {name}：固定值清单，共{len(values)}项，"
                f"哈希{_stable_hash(values)[:16]}"
            )
        return "\n".join(items) or "- 没有识别到可安全读取的固定映射或清单"


def audit_safe_python_formulas(
    markdown: str,
    spec: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Compare model formulas with simple formulas statically present in Skill code.

    Uploaded code is only parsed. It is never imported or executed. Outputs that
    have no matching, safely readable assignment remain visibly "not checked";
    a mismatching assignment fails closed before rule activation.
    """

    if str(spec.get("schema_version") or "") != "2":
        return ()
    assignments = _python_assignments(str(markdown or ""))
    symbol_refs: dict[str, tuple[str, str]] = {}
    for metric in spec.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        key = str(metric.get("key") or "").strip()
        symbol = str(metric.get("source_symbol") or key).strip()
        if key and _safe_identifier(symbol):
            symbol_refs[symbol] = ("metric", key)
    for output in spec.get("outputs") or []:
        if not isinstance(output, dict):
            continue
        key = str(output.get("key") or "").strip()
        symbol = str(output.get("source_symbol") or key).strip()
        if key and _safe_identifier(symbol):
            symbol_refs.setdefault(symbol, ("result", key))

    audits: list[dict[str, Any]] = []
    for output in spec.get("outputs") or []:
        if not isinstance(output, dict):
            continue
        key = str(output.get("key") or "").strip()
        name = str(output.get("name") or key or "未命名结果")
        explicit_symbol = str(output.get("source_symbol") or "").strip()
        source_symbol = explicit_symbol or key
        matches = assignments.get(source_symbol, [])
        base = {
            "result_key": key,
            "result_name": name,
            "source_symbol": source_symbol,
        }
        if not matches:
            if explicit_symbol:
                audits.append(
                    {
                        **base,
                        "status": "failed",
                        "status_label": "未通过",
                        "source_line": None,
                        "message": (
                            f"Skill 代码中没有找到变量 {source_symbol}，"
                            "请核对该结果对应的代码变量"
                        ),
                    }
                )
            else:
                audits.append(
                    {
                        **base,
                        "status": "not_checked",
                        "status_label": "需人工核对",
                        "source_line": None,
                        "message": "Skill 中没有可安全静态比对的同名公式",
                    }
                )
            continue
        if len(matches) != 1:
            audits.append(
                {
                    **base,
                    "status": "failed",
                    "status_label": "未通过",
                    "source_line": matches[0][1],
                    "message": (
                        f"Skill 代码中变量 {source_symbol} 有多处赋值，"
                        "系统不能确定哪一处是当前公式"
                    ),
                }
            )
            continue
        source_node, source_line = matches[0]
        unknown_symbols: set[str] = set()
        try:
            source_expression, source_zero_policy = _canonical_python_expression(
                source_node,
                symbol_refs,
                unknown_symbols,
            )
            fixed_expression = _canonical_fixed_expression(
                output.get("expression"),
            )
        except RuleLiteralError as exc:
            audits.append(
                {
                    **base,
                    "status": "not_checked",
                    "status_label": "需人工核对",
                    "source_line": source_line,
                    "message": (
                        f"该代码赋值不是简单算术公式，暂不能静态比对：{exc}"
                    ),
                }
            )
            continue
        if unknown_symbols:
            audits.append(
                {
                    **base,
                    "status": "failed",
                    "status_label": "未通过",
                    "source_line": source_line,
                    "message": (
                        "固定公式尚未说明这些 Skill 代码变量对应哪个基础指标："
                        + "、".join(sorted(unknown_symbols))
                    ),
                }
            )
            continue
        fixed_zero_policy = str(
            output.get("on_divide_by_zero", "error")
        ).strip()
        zero_matches = (
            source_zero_policy is None
            or fixed_zero_policy == source_zero_policy
        )
        if source_expression != fixed_expression or not zero_matches:
            audits.append(
                {
                    **base,
                    "status": "failed",
                    "status_label": "未通过",
                    "source_line": source_line,
                    "message": (
                        "固定公式与 Skill 代码不一致，可能遗漏倍率、"
                        "单位换算、变量对应或除零处理"
                    ),
                }
            )
            continue
        audits.append(
            {
                **base,
                "status": "passed",
                "status_label": "已通过",
                "source_line": source_line,
                "message": "固定公式与 Skill 代码中的安全算术表达式一致",
            }
        )
    return tuple(audits)


def extract_safe_rule_literals(markdown: str) -> SafeRuleLiteralCatalog:
    """Read literal dictionaries and collections from Python code fences.

    The code is parsed but never imported or executed. Only top-level assignments
    accepted by ``ast.literal_eval`` survive, and only scalar-to-scalar mappings or
    scalar collections are returned.
    """

    mappings: dict[str, dict[str, str]] = {}
    collections: dict[str, tuple[str, ...]] = {}
    for code in _python_code_blocks(str(markdown or "")):
        if len(code) > 2_000_000:
            continue
        try:
            module = ast.parse(code)
        except SyntaxError:
            continue
        for statement in module.body:
            name, value_node = _literal_assignment(statement)
            if not name or value_node is None:
                continue
            try:
                value = ast.literal_eval(value_node)
            except (ValueError, TypeError, MemoryError, RecursionError):
                continue
            if isinstance(value, dict):
                normalized = _safe_mapping(value)
                if normalized is not None and len(normalized) <= 20_000:
                    mappings.setdefault(name, normalized)
            elif isinstance(value, (set, frozenset, list, tuple)):
                normalized_values = _safe_collection(value)
                if normalized_values is not None and len(normalized_values) <= 5_000:
                    collections.setdefault(name, normalized_values)
    return SafeRuleLiteralCatalog(mappings, collections)


def redact_safe_rule_literals_for_model(markdown: str) -> str:
    """Keep line numbers while replacing large literal bodies with hash summaries."""

    source_lines = str(markdown or "").splitlines()
    output_lines = list(source_lines)
    catalog = extract_safe_rule_literals(markdown)
    block_start: int | None = None
    for index, line in enumerate(source_lines):
        stripped = line.strip()
        if block_start is None and stripped.startswith("```"):
            language = stripped[3:].strip().casefold()
            block_start = index + 1 if language in {"python", "py"} else -1
            continue
        if block_start is None:
            continue
        if stripped != "```":
            continue
        if block_start >= 0:
            code = "\n".join(source_lines[block_start:index])
            try:
                module = ast.parse(code)
            except SyntaxError:
                module = None
            for statement in module.body if module is not None else []:
                name, value_node = _literal_assignment(statement)
                if not name or value_node is None:
                    continue
                if name in catalog.mappings:
                    count = len(catalog.mappings[name])
                    content_hash = _stable_hash(catalog.mappings[name])[:16]
                    label = "精确映射"
                elif name in catalog.collections:
                    count = len(catalog.collections[name])
                    content_hash = _stable_hash(catalog.collections[name])[:16]
                    label = "固定值清单"
                else:
                    continue
                first = block_start + int(statement.lineno) - 1
                last = block_start + int(
                    getattr(statement, "end_lineno", statement.lineno)
                ) - 1
                output_lines[first] = (
                    f"{name} = "
                    f'"<{label}由系统安全读取，共{count}条，哈希{content_hash}>"'
                )
                for body_index in range(first + 1, min(last + 1, index)):
                    output_lines[body_index] = ""
        block_start = None
    return "\n".join(output_lines)


def materialize_rule_literal_refs(
    spec: dict[str, Any],
    catalog: SafeRuleLiteralCatalog,
) -> dict[str, Any]:
    """Replace reviewed literal references with their immutable values."""

    materialized = deepcopy(spec)
    if str(materialized.get("schema_version") or "") != "2":
        return materialized
    subject = materialized.get("subject")
    if not isinstance(subject, dict):
        return materialized
    group_refs = _literal_ref_names(
        subject.pop("exclude_values_ref", ""),
        subject.pop("exclude_values_refs", []),
    )
    if group_refs:
        subject["exclude_values"] = _combined_collection_refs(
            subject.get("exclude_values", []),
            group_refs,
            catalog,
        )
    lookups = subject.get("lookups")
    if not isinstance(lookups, dict):
        return materialized
    for lookup in lookups.values():
        if not isinstance(lookup, dict):
            continue
        exclusion_refs = _literal_ref_names(
            lookup.pop("exclude_source_values_ref", ""),
            lookup.pop("exclude_source_values_refs", []),
        )
        if exclusion_refs:
            lookup["exclude_source_values"] = _combined_collection_refs(
                lookup.get("exclude_source_values", []),
                exclusion_refs,
                catalog,
            )
        for step in lookup.get("steps") or []:
            if not isinstance(step, dict):
                continue
            mapping_ref = str(step.pop("mapping_ref", "") or "")
            if not mapping_ref:
                continue
            if "mapping" in step:
                raise RuleLiteralError(
                    f"精确映射步骤同时声明了 mapping 和 mapping_ref：{mapping_ref}"
                )
            if mapping_ref not in catalog.mappings:
                raise RuleLiteralError(
                    f"SKILL.md 中没有可安全读取的固定映射：{mapping_ref}"
                )
            step["mapping"] = dict(catalog.mappings[mapping_ref])
            step["mapping_ref_evidence"] = mapping_ref
    return materialized


def _python_code_blocks(markdown: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] | None = None
    accepted = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if current is None and stripped.startswith("```"):
            language = stripped[3:].strip().casefold()
            current = []
            accepted = language in {"python", "py"}
            continue
        if current is not None and stripped == "```":
            if accepted:
                blocks.append("\n".join(current))
            current = None
            accepted = False
            continue
        if current is not None:
            current.append(line)
    return blocks


def _python_code_blocks_with_lines(markdown: str) -> list[tuple[str, int]]:
    blocks: list[tuple[str, int]] = []
    current: list[str] | None = None
    accepted = False
    start_line = 1
    for line_number, line in enumerate(markdown.splitlines(), start=1):
        stripped = line.strip()
        if current is None and stripped.startswith("```"):
            language = stripped[3:].strip().casefold()
            current = []
            accepted = language in {"python", "py"}
            start_line = line_number + 1
            continue
        if current is not None and stripped == "```":
            if accepted:
                blocks.append(("\n".join(current), start_line))
            current = None
            accepted = False
            continue
        if current is not None:
            current.append(line)
    return blocks


def _python_assignments(
    markdown: str,
) -> dict[str, list[tuple[ast.expr, int]]]:
    assignments: dict[str, list[tuple[ast.expr, int]]] = {}
    for code, start_line in _python_code_blocks_with_lines(markdown):
        if len(code) > 2_000_000:
            continue
        try:
            module = ast.parse(code)
        except SyntaxError:
            continue
        for statement in ast.walk(module):
            name, value_node = _literal_assignment(statement)
            if not name or value_node is None:
                continue
            source_line = start_line + int(statement.lineno) - 1
            assignments.setdefault(name, []).append((value_node, source_line))
    return assignments


def _safe_identifier(value: str) -> bool:
    return bool(value and value.isidentifier() and not keyword.iskeyword(value))


def _canonical_python_expression(
    node: ast.expr,
    symbol_refs: dict[str, tuple[str, str]],
    unknown_symbols: set[str],
) -> tuple[Any, str | None]:
    zero_policy: str | None = None
    if isinstance(node, ast.IfExp):
        if not _ast_numeric_zero(node.orelse):
            raise RuleLiteralError("条件公式的默认值不是零")
        node = node.body
        zero_policy = "zero"
    return (
        _canonical_python_node(node, symbol_refs, unknown_symbols),
        zero_policy,
    )


def _canonical_python_node(
    node: ast.expr,
    symbol_refs: dict[str, tuple[str, str]],
    unknown_symbols: set[str],
) -> Any:
    if isinstance(node, ast.Name):
        reference = symbol_refs.get(node.id)
        if reference is None:
            unknown_symbols.add(node.id)
            return ("unknown", node.id)
        return reference
    if isinstance(node, ast.Constant) and isinstance(
        node.value,
        (int, float, str),
    ):
        return ("value", _normalized_number(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _canonical_python_node(
            node.operand,
            symbol_refs,
            unknown_symbols,
        )
        return _canonical_operation(
            "multiply",
            (("value", "-1"), value),
        )
    if isinstance(node, ast.BinOp):
        operation = {
            ast.Add: "add",
            ast.Sub: "subtract",
            ast.Mult: "multiply",
            ast.Div: "divide",
        }.get(type(node.op))
        if operation is None:
            raise RuleLiteralError("包含不受支持的算术运算")
        return _canonical_operation(
            operation,
            (
                _canonical_python_node(
                    node.left,
                    symbol_refs,
                    unknown_symbols,
                ),
                _canonical_python_node(
                    node.right,
                    symbol_refs,
                    unknown_symbols,
                ),
            ),
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"abs", "min", "max"}
        and not node.keywords
    ):
        if node.func.id == "abs" and len(node.args) != 1:
            raise RuleLiteralError("绝对值参数数量无效")
        return _canonical_operation(
            node.func.id,
            tuple(
                _canonical_python_node(
                    argument,
                    symbol_refs,
                    unknown_symbols,
                )
                for argument in node.args
            ),
        )
    raise RuleLiteralError("包含不受支持的代码表达式")


def _canonical_fixed_expression(expression: Any) -> Any:
    if not isinstance(expression, dict):
        raise RuleLiteralError("固定公式不是结构化表达式")
    choices = [
        key
        for key in ("value", "metric", "result", "op")
        if key in expression
    ]
    if len(choices) != 1:
        raise RuleLiteralError("固定公式节点不完整")
    if "value" in expression:
        return ("value", _normalized_number(expression["value"]))
    if "metric" in expression:
        return ("metric", str(expression["metric"]))
    if "result" in expression:
        return ("result", str(expression["result"]))
    operation = str(expression["op"])
    arguments = expression.get("args")
    if not isinstance(arguments, list):
        raise RuleLiteralError("固定公式参数无效")
    return _canonical_operation(
        operation,
        tuple(_canonical_fixed_expression(argument) for argument in arguments),
    )


def _canonical_operation(operation: str, arguments: tuple[Any, ...]) -> Any:
    if operation in {"add", "multiply"}:
        flattened: list[Any] = []
        for argument in arguments:
            if (
                isinstance(argument, tuple)
                and len(argument) == 2
                and argument[0] == operation
                and isinstance(argument[1], tuple)
            ):
                flattened.extend(argument[1])
            else:
                flattened.append(argument)
        return (
            operation,
            tuple(sorted(flattened, key=repr)),
        )
    return (operation, arguments)


def _normalized_number(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RuleLiteralError("公式常量不是数值") from exc
    if not number.is_finite():
        raise RuleLiteralError("公式常量不是有限数值")
    normalized = number.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal(1)))
    return format(normalized, "f")


def _ast_numeric_zero(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and Decimal(str(node.value)) == 0
    )


def _literal_assignment(
    statement: ast.stmt,
) -> tuple[str, ast.expr | None]:
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    ):
        return statement.targets[0].id, statement.value
    if isinstance(statement, ast.AnnAssign) and isinstance(
        statement.target,
        ast.Name,
    ):
        return statement.target.id, statement.value
    return "", None


def _safe_mapping(value: dict[Any, Any]) -> dict[str, str] | None:
    output: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(key, (dict, list, set, tuple)) or isinstance(
            item,
            (dict, list, set, tuple),
        ):
            return None
        normalized_key = str(key).strip()
        normalized_value = str(item).strip()
        if (
            not normalized_key
            or not normalized_value
            or len(normalized_key) > 1_000
            or len(normalized_value) > 1_000
        ):
            return None
        output[normalized_key] = normalized_value
    return output or None


def _safe_collection(value: Any) -> tuple[str, ...] | None:
    output: list[str] = []
    for item in value:
        if isinstance(item, (dict, list, set, tuple)):
            return None
        normalized = str(item).strip()
        if not normalized or len(normalized) > 1_000:
            return None
        if normalized not in output:
            output.append(normalized)
    return tuple(sorted(output)) if output else None


def _collection_ref(
    name: str,
    catalog: SafeRuleLiteralCatalog,
) -> list[str]:
    if name not in catalog.collections:
        raise RuleLiteralError(
            f"SKILL.md 中没有可安全读取的固定值清单：{name}"
        )
    return list(catalog.collections[name])


def _literal_ref_names(single: Any, multiple: Any) -> list[str]:
    output = []
    for value in [single, *(multiple if isinstance(multiple, list) else [])]:
        name = str(value or "").strip()
        if name and name not in output:
            output.append(name)
    return output


def _combined_collection_refs(
    existing: Any,
    refs: list[str],
    catalog: SafeRuleLiteralCatalog,
) -> list[str]:
    values = [
        str(value).strip()
        for value in (existing if isinstance(existing, list) else [])
        if str(value).strip()
    ]
    for ref in refs:
        for value in _collection_ref(ref, catalog):
            if value not in values:
                values.append(value)
    return values


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
