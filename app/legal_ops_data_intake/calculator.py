from __future__ import annotations

import hashlib
import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import (
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Decimal,
    DecimalException,
    InvalidOperation,
)
from typing import Any


class CalculationBlocked(ValueError):
    """The requested calculation cannot safely run."""


@dataclass(frozen=True)
class PersonResult:
    person_id: str
    values: dict[str, Decimal]
    lineage: dict[str, dict[str, Any]]
    display_name: str = ""


@dataclass(frozen=True)
class CalculationResult:
    rule_version: str
    input_hash: str
    output_hash: str
    results: tuple[PersonResult, ...]
    errors: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class LookupStepResult:
    step_name: str
    source_value: str
    mapped_value: str


@dataclass(frozen=True)
class LookupResolution:
    final_value: str | None
    steps: tuple[LookupStepResult, ...] = ()
    error: dict[str, Any] | None = None


_OPS = {"add", "subtract", "multiply", "divide", "min", "max", "abs"}
_COMPARISON_OPS = {
    "eq",
    "neq",
    "lt",
    "lte",
    "gt",
    "gte",
    "in",
    "not_in",
    "is_empty",
    "not_empty",
}
_BOUNDARIES = {
    "period_start",
    "period_end",
    "month_start",
    "month_end",
    "year_start",
    "year_end",
}
_ROUNDING = {"half_up": ROUND_HALF_UP, "half_even": ROUND_HALF_EVEN}
_COLUMN_TYPES = {"text", "date", "decimal", "percentage", "integer"}
_LOOKUP_MATCH_STRATEGIES = {
    "exact",
    "expand_branch_short_form",
    "contains_unique",
    "strip_aftercare_suffix",
    "strip_parenthetical",
}
_DIVIDE_BY_ZERO_POLICIES = {"error", "zero"}
_SUBJECT_SCOPES = {"grouped", "total_only"}
_MAX_TABLES = 64
_MAX_COLUMNS_PER_TABLE = 512
_MAX_METRICS = 256
_MAX_OUTPUTS = 128
_MAX_LOOKUP_STEPS = 8
_MAX_LOOKUP_ENTRIES = 20_000
_MAX_KEY_LENGTH = 128
_MAX_EXPRESSION_DEPTH = 32
_MAX_EXPRESSION_NODES = 4_096


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def calculate_input_hash(
    rule_spec: dict[str, Any],
    sources: dict[str, list[dict[str, Any]]],
    calculation_context: dict[str, Any] | None = None,
) -> str:
    ordered_sources = {
        table: sorted(
            rows,
            key=lambda row: (
                str(row.get("__row_number__", "")),
                json.dumps(_json_value(row), ensure_ascii=False, sort_keys=True),
            ),
        )
        for table, rows in sorted(sources.items())
    }
    return _stable_hash(
        {
            "rule": rule_spec,
            "sources": ordered_sources,
            "calculation_context": calculation_context or {},
        }
    )


def validate_rule_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise CalculationBlocked("规则结构版本无效")
    schema_version = str(spec.get("schema_version") or "")
    if schema_version == "2":
        return _validate_aggregate_rule_spec(spec)
    if schema_version != "1":
        raise CalculationBlocked("规则结构版本无效")
    person_key = str(spec.get("person_key") or "").strip()
    tables = spec.get("source_tables")
    outputs = spec.get("outputs")
    if (
        not person_key
        or len(person_key) > _MAX_KEY_LENGTH
        or not isinstance(tables, list)
        or not tables
        or len(tables) > _MAX_TABLES
    ):
        raise CalculationBlocked("规则未定义稳定人员标识或源表")
    if not isinstance(outputs, list) or not outputs or len(outputs) > _MAX_OUTPUTS:
        raise CalculationBlocked("规则未定义计算结果")

    table_keys: set[str] = set()
    fields: set[str] = set()
    for table in tables:
        if not isinstance(table, dict):
            raise CalculationBlocked("源表定义无效")
        table_key = str(table.get("key") or "").strip()
        table_name = str(table.get("name") or table_key).strip()
        columns = table.get("columns")
        if (
            not table_key
            or len(table_key) > _MAX_KEY_LENGTH
            or len(table_name) > 256
            or table_key in table_keys
            or not isinstance(columns, list)
            or len(columns) > _MAX_COLUMNS_PER_TABLE
        ):
            raise CalculationBlocked("源表标识重复或字段定义无效")
        table_keys.add(table_key)
        column_keys: set[str] = set()
        for column in columns:
            key = (
                str(column.get("key") or "").strip() if isinstance(column, dict) else ""
            )
            name = (
                str(column.get("name") or key).strip()
                if isinstance(column, dict)
                else ""
            )
            data_type = (
                str(column.get("type") or "text") if isinstance(column, dict) else ""
            )
            if (
                not key
                or len(key) > _MAX_KEY_LENGTH
                or len(name) > 256
                or data_type not in _COLUMN_TYPES
                or key in column_keys
            ):
                raise CalculationBlocked(f"源表 {table_key} 的字段标识重复或为空")
            column_keys.add(key)
            fields.add(f"{table_key}.{key}")
        if person_key not in column_keys:
            raise CalculationBlocked(f"源表 {table_key} 缺少稳定人员标识 {person_key}")

    output_keys: set[str] = set()
    for output in outputs:
        if not isinstance(output, dict):
            raise CalculationBlocked("结果定义无效")
        output_key = str(output.get("key") or "").strip()
        output_name = str(output.get("name") or output_key).strip()
        if (
            not output_key
            or len(output_key) > _MAX_KEY_LENGTH
            or len(output_name) > 256
            or output_key in output_keys
        ):
            raise CalculationBlocked("结果标识重复或为空")
        output_keys.add(output_key)
        places = output.get("decimal_places", 2)
        if not isinstance(places, int) or places < 0 or places > 8:
            raise CalculationBlocked(f"结果 {output_key} 的小数位数无效")
        if output.get("rounding", "half_up") not in _ROUNDING:
            raise CalculationBlocked(f"结果 {output_key} 的取整方式不受支持")
        _validate_expression(output.get("expression"), fields, depth=0, budget=[0])
    return spec


def aggregate_table_row_key(
    spec: dict[str, Any],
    table: dict[str, Any],
) -> str:
    """Return a table's stable record key, with the v2 top-level default."""

    return str(table.get("row_key") or spec.get("row_key") or "").strip()


def aggregate_row_relevant_to_any_metric(
    spec: dict[str, Any],
    *,
    table_key: str,
    field_key: str,
    row: dict[str, Any],
    period_start: date | str | None,
    period_end: date | str | None,
) -> bool:
    """Return whether one missing field can affect a selected-period metric."""

    if str(spec.get("schema_version") or "") != "2":
        return True
    start = _coerce_period_date(period_start, "考核周期开始日期")
    end = _coerce_period_date(period_end, "考核周期结束日期")
    if end < start:
        raise CalculationBlocked("考核周期结束日期不能早于开始日期")
    boundaries = _date_boundaries(start, end)
    metrics = [
        metric
        for metric in spec.get("metrics") or []
        if isinstance(metric, dict)
        and str(metric.get("source_table") or "") == table_key
    ]
    if not metrics:
        return False
    field_ref = f"{table_key}.{field_key}"
    for metric in metrics:
        where = metric.get("where")
        condition_fields = _collect_fields(where)
        value_fields = _collect_fields(metric.get("value"))
        if field_ref not in condition_fields and field_ref not in value_fields:
            continue
        matches = where is None or _evaluate_condition(where, row, boundaries)
        if not matches:
            continue
        if field_ref in value_fields and bool(metric.get("null_as_zero", False)):
            continue
        if field_ref in condition_fields or field_ref in value_fields:
            return True
    return False


def _validate_aggregate_rule_spec(spec: dict[str, Any]) -> dict[str, Any]:
    row_key = str(spec.get("row_key") or "").strip()
    tables = spec.get("source_tables")
    metrics = spec.get("metrics")
    outputs = spec.get("outputs")
    subject = spec.get("subject")
    if (
        len(row_key) > _MAX_KEY_LENGTH
        or not isinstance(tables, list)
        or not tables
        or len(tables) > _MAX_TABLES
    ):
        raise CalculationBlocked("汇总规则未定义稳定记录标识或源表")
    if not isinstance(subject, dict):
        raise CalculationBlocked("汇总规则未定义核算对象")
    if not isinstance(metrics, list) or not metrics or len(metrics) > _MAX_METRICS:
        raise CalculationBlocked("汇总规则未定义基础指标")
    if not isinstance(outputs, list) or not outputs or len(outputs) > _MAX_OUTPUTS:
        raise CalculationBlocked("汇总规则未定义计算结果")

    table_keys: set[str] = set()
    table_columns: dict[str, set[str]] = {}
    all_fields: set[str] = set()
    for table in tables:
        if not isinstance(table, dict):
            raise CalculationBlocked("源表定义无效")
        table_key = str(table.get("key") or "").strip()
        table_name = str(table.get("name") or table_key).strip()
        columns = table.get("columns")
        if (
            not table_key
            or len(table_key) > _MAX_KEY_LENGTH
            or len(table_name) > 256
            or table_key in table_keys
            or not isinstance(columns, list)
            or not columns
            or len(columns) > _MAX_COLUMNS_PER_TABLE
            or not isinstance(table.get("allow_extra_columns", False), bool)
        ):
            raise CalculationBlocked("源表标识重复或字段定义无效")
        table_keys.add(table_key)
        table_row_key = aggregate_table_row_key(spec, table)
        if not table_row_key or len(table_row_key) > _MAX_KEY_LENGTH:
            raise CalculationBlocked(
                f"源表 {table_key} 未定义稳定记录标识"
            )
        column_keys: set[str] = set()
        for column in columns:
            key = (
                str(column.get("key") or "").strip() if isinstance(column, dict) else ""
            )
            name = (
                str(column.get("name") or key).strip()
                if isinstance(column, dict)
                else ""
            )
            data_type = (
                str(column.get("type") or "text") if isinstance(column, dict) else ""
            )
            if (
                not key
                or len(key) > _MAX_KEY_LENGTH
                or len(name) > 256
                or data_type not in _COLUMN_TYPES
                or key in column_keys
            ):
                raise CalculationBlocked(
                    f"源表 {table_key} 的字段标识重复、为空或类型无效"
                )
            column_keys.add(key)
            all_fields.add(f"{table_key}.{key}")
        if table_row_key not in column_keys:
            raise CalculationBlocked(
                f"源表 {table_key} 缺少稳定记录标识 {table_row_key}"
            )
        table_columns[table_key] = column_keys

    subject_key = str(subject.get("key") or "").strip()
    subject_name = str(subject.get("name") or subject_key).strip()
    subject_fields = subject.get("fields", {})
    subject_lookups = subject.get("lookups", {})
    include_total = subject.get("include_total", False)
    exclude_values = subject.get("exclude_values", [])
    if (
        not subject_key
        or len(subject_key) > _MAX_KEY_LENGTH
        or not subject_name
        or len(subject_name) > 256
        or not isinstance(subject_fields, dict)
        or not isinstance(subject_lookups, dict)
        or not isinstance(include_total, bool)
        or not isinstance(subject.get("exclude_empty", True), bool)
        or not isinstance(exclude_values, list)
        or len(exclude_values) > 256
        or any(not isinstance(value, (str, int, float)) for value in exclude_values)
    ):
        raise CalculationBlocked("核算对象定义无效")
    if not subject_fields and not subject_lookups and not include_total:
        raise CalculationBlocked("核算对象必须提供分组字段、精确映射或整体结果")
    overlapping_resolvers = {
        str(table_key) for table_key in subject_fields
    }.intersection(str(table_key) for table_key in subject_lookups)
    if overlapping_resolvers:
        raise CalculationBlocked(
            "同一源表不能同时按底表字段和固定映射确定核算对象："
            + "、".join(sorted(overlapping_resolvers))
        )
    for table_key, column_key in subject_fields.items():
        if (
            str(table_key) not in table_columns
            or str(column_key) not in table_columns[str(table_key)]
        ):
            raise CalculationBlocked(
                f"核算对象引用了未定义字段：{table_key}.{column_key}"
            )
    lookup_entry_count = 0
    for table_key, lookup in subject_lookups.items():
        table_key = str(table_key)
        if table_key not in table_columns or not isinstance(lookup, dict):
            raise CalculationBlocked("核算对象精确映射引用了未定义源表")
        source_field = str(lookup.get("source_field") or "").strip()
        steps = lookup.get("steps")
        exclusions = lookup.get("exclude_source_values", [])
        if (
            source_field not in table_columns[table_key]
            or not isinstance(steps, list)
            or not steps
            or len(steps) > _MAX_LOOKUP_STEPS
            or lookup.get("on_unmatched", "error") != "error"
            or not isinstance(exclusions, list)
            or len(exclusions) > 2_000
            or any(isinstance(value, (dict, list)) for value in exclusions)
        ):
            raise CalculationBlocked(f"源表 {table_key} 的精确映射定义无效")
        for step in steps:
            if not isinstance(step, dict):
                raise CalculationBlocked("精确映射步骤定义无效")
            step_name = str(step.get("name") or "").strip()
            mapping = step.get("mapping")
            normalizers = step.get("normalizers", [])
            match_strategies = step.get("match_strategies", [])
            separator = str(step.get("split_first") or "")
            if (
                not step_name
                or len(step_name) > 256
                or not isinstance(mapping, dict)
                or not mapping
                or not isinstance(normalizers, list)
                or not isinstance(match_strategies, list)
                or any(
                    value not in {"remove_whitespace", "normalize_parentheses", "casefold"}
                    for value in normalizers
                )
                or len(normalizers) != len(set(normalizers))
                or any(
                    value not in _LOOKUP_MATCH_STRATEGIES
                    for value in match_strategies
                )
                or len(match_strategies) != len(set(match_strategies))
                or len(match_strategies) > len(_LOOKUP_MATCH_STRATEGIES)
                or (separator and (len(separator) != 1 or separator not in "/、,，"))
            ):
                raise CalculationBlocked(f"精确映射步骤“{step_name or '未命名'}”无效")
            lookup_entry_count += len(mapping)
            if lookup_entry_count > _MAX_LOOKUP_ENTRIES:
                raise CalculationBlocked("精确映射条目超过安全上限")
            normalized_keys: set[str] = set()
            for key, value in mapping.items():
                if (
                    isinstance(key, (dict, list))
                    or isinstance(value, (dict, list))
                    or not str(key).strip()
                    or not str(value).strip()
                    or len(str(key)) > 1_000
                    or len(str(value)) > 1_000
                ):
                    raise CalculationBlocked(f"精确映射步骤“{step_name}”包含无效条目")
                normalized_key = _normalize_lookup_value(
                    str(key),
                    normalizers,
                )
                if normalized_key in normalized_keys:
                    raise CalculationBlocked(
                        f"精确映射步骤“{step_name}”在标准化后出现重复来源值"
                    )
                normalized_keys.add(normalized_key)
    total_key = str(subject.get("total_key") or "__total__").strip()
    total_label = str(subject.get("total_label") or "整体").strip()
    if (
        include_total
        and (
            not total_key
            or len(total_key) > _MAX_KEY_LENGTH
            or not total_label
            or len(total_label) > 256
        )
    ):
        raise CalculationBlocked("整体核算对象定义无效")

    metric_keys: set[str] = set()
    metric_scopes: dict[str, str] = {}
    for metric in metrics:
        if not isinstance(metric, dict):
            raise CalculationBlocked("基础指标定义无效")
        metric_key = str(metric.get("key") or "").strip()
        metric_name = str(metric.get("name") or metric_key).strip()
        source_table = str(metric.get("source_table") or "").strip()
        aggregate = str(metric.get("aggregate") or "").strip()
        subject_scope = str(metric.get("subject_scope") or "grouped").strip()
        source_symbol = str(metric.get("source_symbol") or "").strip()
        filter_source_symbol = str(
            metric.get("filter_source_symbol") or ""
        ).strip()
        null_as_zero = metric.get("null_as_zero", False)
        if (
            not metric_key
            or len(metric_key) > _MAX_KEY_LENGTH
            or not metric_name
            or len(metric_name) > 256
            or metric_key in metric_keys
            or source_table not in table_keys
            or aggregate not in {"count", "sum"}
            or not isinstance(null_as_zero, bool)
            or subject_scope not in _SUBJECT_SCOPES
            or (subject_scope == "total_only" and not include_total)
            or (
                source_symbol
                and (
                    len(source_symbol) > _MAX_KEY_LENGTH
                    or not source_symbol.isidentifier()
                )
            )
            or (
                filter_source_symbol
                and (
                    len(filter_source_symbol) > _MAX_KEY_LENGTH
                    or not filter_source_symbol.isidentifier()
                )
            )
        ):
            raise CalculationBlocked("基础指标标识、来源表或汇总方式无效")
        if (
            source_table not in subject_fields
            and source_table not in subject_lookups
            and not include_total
        ):
            raise CalculationBlocked(
                f"基础指标 {metric_key} 的来源表没有核算对象分组字段"
            )
        metric_keys.add(metric_key)
        metric_scopes[metric_key] = subject_scope
        table_fields = {
            field for field in all_fields if field.startswith(f"{source_table}.")
        }
        where = metric.get("where")
        if where is not None:
            _validate_condition(where, table_fields, depth=0, budget=[0])
        value_expression = metric.get("value")
        if aggregate == "sum":
            if value_expression is None:
                raise CalculationBlocked(f"求和指标 {metric_key} 缺少取值字段")
            _validate_expression(
                value_expression,
                table_fields,
                depth=0,
                budget=[0],
            )
        elif value_expression is not None:
            raise CalculationBlocked(f"计数指标 {metric_key} 不应包含求和值")
        elif null_as_zero:
            raise CalculationBlocked(
                f"计数指标 {metric_key} 不应设置空值按零计算"
            )

    output_keys: set[str] = set()
    output_scopes: dict[str, str] = {}
    for output in outputs:
        if not isinstance(output, dict):
            raise CalculationBlocked("结果定义无效")
        output_key = str(output.get("key") or "").strip()
        output_name = str(output.get("name") or output_key).strip()
        subject_scope = str(output.get("subject_scope") or "grouped").strip()
        source_symbol = str(output.get("source_symbol") or "").strip()
        unit = str(output.get("unit") or "").strip()
        if (
            not output_key
            or len(output_key) > _MAX_KEY_LENGTH
            or len(output_name) > 256
            or output_key in output_keys
            or subject_scope not in _SUBJECT_SCOPES
            or (subject_scope == "total_only" and not include_total)
            or len(unit) > 64
            or (
                source_symbol
                and (
                    len(source_symbol) > _MAX_KEY_LENGTH
                    or not source_symbol.isidentifier()
                )
            )
        ):
            raise CalculationBlocked("结果标识重复或为空")
        output_keys.add(output_key)
        output_scopes[output_key] = subject_scope
        places = output.get("decimal_places", 2)
        if not isinstance(places, int) or places < 0 or places > 8:
            raise CalculationBlocked(f"结果 {output_key} 的小数位数无效")
        if output.get("rounding", "half_up") not in _ROUNDING:
            raise CalculationBlocked(f"结果 {output_key} 的取整方式不受支持")
        if output.get("on_divide_by_zero", "error") not in (
            _DIVIDE_BY_ZERO_POLICIES
        ):
            raise CalculationBlocked(f"结果 {output_key} 的除零处理方式不受支持")
        _validate_metric_expression(
            output.get("expression"),
            metric_keys,
            output_keys - {output_key},
            depth=0,
            budget=[0],
        )
        referenced_metrics, referenced_results = (
            _metric_expression_references(output.get("expression"))
        )
        if subject_scope == "grouped" and (
            any(metric_scopes[key] == "total_only" for key in referenced_metrics)
            or any(
                output_scopes[key] == "total_only"
                for key in referenced_results
            )
        ):
            raise CalculationBlocked(
                f"结果 {output_key} 按团队展示，"
                "但引用的指标只能用于整体结果"
            )
    return spec


def _validate_condition(
    condition: Any,
    fields: set[str],
    *,
    depth: int,
    budget: list[int],
) -> None:
    budget[0] += 1
    if depth > _MAX_EXPRESSION_DEPTH or budget[0] > _MAX_EXPRESSION_NODES:
        raise CalculationBlocked("筛选条件超过安全复杂度限制")
    if not isinstance(condition, dict):
        raise CalculationBlocked("筛选条件结构无效")
    choices = sum(key in condition for key in ("all", "any", "op"))
    if choices != 1:
        raise CalculationBlocked("筛选条件必须且只能包含全部、任一或一个比较")
    for group_key in ("all", "any"):
        if group_key not in condition:
            continue
        children = condition[group_key]
        if not isinstance(children, list) or not children or len(children) > 256:
            raise CalculationBlocked("筛选条件组合为空或过多")
        for child in children:
            _validate_condition(
                child,
                fields,
                depth=depth + 1,
                budget=budget,
            )
        return
    op = str(condition.get("op") or "")
    if op not in _COMPARISON_OPS:
        raise CalculationBlocked(f"筛选比较方式不受支持：{op or '空'}")
    _validate_operand(condition.get("left"), fields, allow_values=False)
    if op in {"is_empty", "not_empty"}:
        if "right" in condition:
            raise CalculationBlocked(f"筛选比较 {op} 不应包含右侧值")
        return
    _validate_operand(
        condition.get("right"),
        fields,
        allow_values=op in {"in", "not_in"},
    )


def _validate_operand(
    operand: Any,
    fields: set[str],
    *,
    allow_values: bool,
) -> None:
    if not isinstance(operand, dict):
        raise CalculationBlocked("筛选值结构无效")
    allowed_keys = ("field", "value", "boundary", "values")
    if sum(key in operand for key in allowed_keys) != 1:
        raise CalculationBlocked("筛选值必须且只能引用字段、常量、日期边界或常量列表")
    if "field" in operand:
        field = str(operand["field"])
        if field not in fields:
            raise CalculationBlocked(f"筛选条件引用了未定义字段：{field}")
        return
    if "boundary" in operand:
        boundary = str(operand["boundary"])
        if boundary not in _BOUNDARIES:
            raise CalculationBlocked(f"日期边界不受支持：{boundary}")
        shift = operand.get("shift", {})
        if not isinstance(shift, dict) or set(shift).difference(
            {"years", "months", "days"}
        ):
            raise CalculationBlocked("日期边界偏移定义无效")
        for key, limit in (("years", 50), ("months", 600), ("days", 3_660)):
            value = shift.get(key, 0)
            if not isinstance(value, int) or abs(value) > limit:
                raise CalculationBlocked("日期边界偏移超过安全范围")
        return
    if "values" in operand:
        values = operand["values"]
        if (
            not allow_values
            or not isinstance(values, list)
            or not values
            or len(values) > 256
            or any(isinstance(value, (dict, list)) for value in values)
        ):
            raise CalculationBlocked("筛选常量列表无效")
        return
    if isinstance(operand.get("value"), (dict, list)):
        raise CalculationBlocked("筛选常量类型无效")


def _validate_metric_expression(
    expression: Any,
    metrics: set[str],
    results: set[str],
    *,
    depth: int,
    budget: list[int],
) -> None:
    budget[0] += 1
    if depth > _MAX_EXPRESSION_DEPTH or budget[0] > _MAX_EXPRESSION_NODES:
        raise CalculationBlocked("公式结构超过安全复杂度限制")
    if not isinstance(expression, dict):
        raise CalculationBlocked("公式结构无效")
    choices = sum(key in expression for key in ("value", "metric", "result", "op"))
    if choices != 1:
        raise CalculationBlocked(
            "公式节点必须且只能包含常量、基础指标、前序结果或运算之一"
        )
    if "value" in expression:
        _validated_decimal(expression["value"], "公式常量")
        return
    if "metric" in expression:
        metric = str(expression["metric"])
        if metric not in metrics:
            raise CalculationBlocked(f"公式引用了未定义基础指标：{metric}")
        return
    if "result" in expression:
        result = str(expression["result"])
        if result not in results:
            raise CalculationBlocked(f"公式引用了尚未计算的前序结果：{result}")
        return
    op = str(expression.get("op") or "")
    args = expression.get("args")
    if op not in _OPS or not isinstance(args, list):
        raise CalculationBlocked(f"公式运算不受支持：{op or '空'}")
    if op == "abs" and len(args) != 1:
        raise CalculationBlocked("运算 abs 只能有一个参数")
    if op != "abs" and len(args) < 2:
        raise CalculationBlocked(f"公式运算 {op} 的参数不足")
    if op in {"subtract", "divide"} and len(args) != 2:
        raise CalculationBlocked(f"运算 {op} 只能有两个参数")
    for argument in args:
        _validate_metric_expression(
            argument,
            metrics,
            results,
            depth=depth + 1,
            budget=budget,
        )


def _metric_expression_references(
    expression: Any,
) -> tuple[set[str], set[str]]:
    metrics: set[str] = set()
    results: set[str] = set()
    if not isinstance(expression, dict):
        return metrics, results
    if "metric" in expression:
        metrics.add(str(expression["metric"]))
    if "result" in expression:
        results.add(str(expression["result"]))
    for argument in expression.get("args") or []:
        child_metrics, child_results = _metric_expression_references(argument)
        metrics.update(child_metrics)
        results.update(child_results)
    return metrics, results


def _validated_decimal(value: Any, label: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CalculationBlocked(f"{label}不是合法数字") from None
    if (
        not decimal.is_finite()
        or len(decimal.as_tuple().digits) > 1_000
        or abs(decimal.adjusted()) > 1_000
    ):
        raise CalculationBlocked(f"{label}超过安全数值范围")
    return decimal


def _validate_expression(
    expression: Any,
    fields: set[str],
    *,
    depth: int,
    budget: list[int],
) -> None:
    budget[0] += 1
    if depth > _MAX_EXPRESSION_DEPTH or budget[0] > _MAX_EXPRESSION_NODES:
        raise CalculationBlocked("公式结构超过安全复杂度限制")
    if not isinstance(expression, dict):
        raise CalculationBlocked("公式结构无效")
    choices = sum(key in expression for key in ("value", "field", "op"))
    if choices != 1:
        raise CalculationBlocked("公式节点必须且只能包含常量、字段或运算之一")
    if "value" in expression:
        try:
            value = Decimal(str(expression["value"]))
        except (InvalidOperation, ValueError):
            raise CalculationBlocked("公式常量不是合法数字") from None
        if (
            not value.is_finite()
            or len(value.as_tuple().digits) > 1_000
            or abs(value.adjusted()) > 1_000
        ):
            raise CalculationBlocked("公式常量超过安全数值范围")
        return
    if "field" in expression:
        if str(expression["field"]) not in fields:
            raise CalculationBlocked(f"公式引用了未定义字段：{expression['field']}")
        return
    op = str(expression.get("op") or "")
    args = expression.get("args")
    if op not in _OPS or not isinstance(args, list):
        raise CalculationBlocked(f"公式运算不受支持：{op or '空'}")
    if op == "abs" and len(args) != 1:
        raise CalculationBlocked("运算 abs 只能有一个参数")
    if op != "abs" and len(args) < 2:
        raise CalculationBlocked(f"公式运算 {op} 的参数不足")
    if op in {"subtract", "divide"} and len(args) != 2:
        raise CalculationBlocked(f"运算 {op} 只能有两个参数")
    for argument in args:
        _validate_expression(
            argument,
            fields,
            depth=depth + 1,
            budget=budget,
        )


class DeterministicCalculator:
    def __init__(self, rule_spec: dict[str, Any] | None, *, rule_version: str):
        self.rule_spec = rule_spec
        self.rule_version = rule_version

    def calculate(
        self,
        sources: dict[str, list[dict[str, Any]]],
        *,
        period_start: date | str | None = None,
        period_end: date | str | None = None,
    ) -> CalculationResult:
        if self.rule_spec is None or not self.rule_version:
            raise CalculationBlocked("规则文件尚未配置，暂不可计算")
        spec = validate_rule_spec(self.rule_spec)
        if str(spec["schema_version"]) == "2":
            return _calculate_aggregate_rule(
                spec,
                sources,
                rule_version=self.rule_version,
                period_start=period_start,
                period_end=period_end,
            )
        required = {
            str(table["key"])
            for table in spec["source_tables"]
            if bool(table.get("required", True))
        }
        missing = sorted(required.difference(sources))
        if missing:
            raise CalculationBlocked(f"缺少必需表格：{', '.join(missing)}")

        person_key = str(spec["person_key"])
        by_table: dict[str, dict[str, dict[str, Any]]] = {}
        for table in spec["source_tables"]:
            table_key = str(table["key"])
            if table_key not in sources:
                continue
            indexed: dict[str, dict[str, Any]] = {}
            for row in sources[table_key]:
                person_id = str(row.get(person_key) or "").strip()
                if not person_id:
                    raise CalculationBlocked(f"源表 {table_key} 存在人员标识为空的记录")
                if person_id in indexed:
                    raise CalculationBlocked(
                        f"源表 {table_key} 的人员标识重复：{person_id}"
                    )
                indexed[person_id] = row
            by_table[table_key] = indexed

        required_people = [set(by_table[key]) for key in required]
        if required_people and any(
            people != required_people[0] for people in required_people[1:]
        ):
            raise CalculationBlocked("必需源表的人员记录无法一一关联")
        people = sorted(required_people[0] if required_people else set())

        results: list[PersonResult] = []
        errors: list[dict[str, Any]] = []
        for person_id in people:
            context = {
                table_key: indexed[person_id]
                for table_key, indexed in by_table.items()
                if person_id in indexed
            }
            values: dict[str, Decimal] = {}
            lineage: dict[str, dict[str, Any]] = {}
            try:
                for output in spec["outputs"]:
                    value, fields_used = _evaluate(output["expression"], context)
                    if not value.is_finite():
                        raise CalculationBlocked("计算结果不是有限数值")
                    places = int(output.get("decimal_places", 2))
                    quantizer = Decimal(1).scaleb(-places)
                    rounded = value.quantize(
                        quantizer,
                        rounding=_ROUNDING[str(output.get("rounding", "half_up"))],
                    )
                    if not rounded.is_finite():
                        raise CalculationBlocked("取整后的计算结果不是有限数值")
                    output_key = str(output["key"])
                    values[output_key] = rounded
                    source_rows = []
                    for table_key in sorted(
                        {field.split(".", 1)[0] for field in fields_used}
                    ):
                        row_number = context[table_key].get("__row_number__")
                        source_rows.append(
                            {"table": table_key, "row_number": row_number}
                        )
                    lineage[output_key] = {
                        "rule_version": self.rule_version,
                        "expression": output["expression"],
                        "fields": sorted(fields_used),
                        "source_rows": source_rows,
                    }
            except CalculationBlocked as exc:
                message = str(exc)
                errors.append(
                    {
                        "person_id": person_id,
                        "code": "person_calculation_error",
                        "message": message,
                        "source_rows": [
                            {
                                "table": table_key,
                                "row_number": row.get("__row_number__"),
                            }
                            for table_key, row in sorted(context.items())
                        ],
                    }
                )
                continue
            except (DecimalException, ArithmeticError, ValueError):
                errors.append(
                    {
                        "person_id": person_id,
                        "code": "person_calculation_error",
                        "message": "计算数值无效或超出可处理范围",
                        "source_rows": [
                            {
                                "table": table_key,
                                "row_number": row.get("__row_number__"),
                            }
                            for table_key, row in sorted(context.items())
                        ],
                    }
                )
                continue
            results.append(PersonResult(person_id, values, lineage))

        calculation_context = _calculation_context(period_start, period_end)
        input_hash = calculate_input_hash(
            spec,
            sources,
            calculation_context=calculation_context,
        )
        output_payload = {
            "results": [
                {
                    "person_id": result.person_id,
                    "display_name": result.display_name,
                    "values": result.values,
                    "lineage": result.lineage,
                }
                for result in results
            ],
            "errors": errors,
        }
        return CalculationResult(
            rule_version=self.rule_version,
            input_hash=input_hash,
            output_hash=_stable_hash(output_payload),
            results=tuple(results),
            errors=tuple(errors),
        )


def _calculate_aggregate_rule(
    spec: dict[str, Any],
    sources: dict[str, list[dict[str, Any]]],
    *,
    rule_version: str,
    period_start: date | str | None,
    period_end: date | str | None,
) -> CalculationResult:
    start = _coerce_period_date(period_start, "考核周期开始日期")
    end = _coerce_period_date(period_end, "考核周期结束日期")
    if end < start:
        raise CalculationBlocked("考核周期结束日期不能早于开始日期")
    boundaries = _date_boundaries(start, end)
    required = {
        str(table["key"])
        for table in spec["source_tables"]
        if bool(table.get("required", True))
    }
    missing = sorted(required.difference(sources))
    if missing:
        raise CalculationBlocked(f"缺少必需表格：{', '.join(missing)}")

    table_definitions = {
        str(table["key"]): table for table in spec["source_tables"]
    }
    table_row_keys = {
        table_key: aggregate_table_row_key(spec, table)
        for table_key, table in table_definitions.items()
    }
    for table_key, rows in sources.items():
        if table_key not in table_definitions:
            continue
        seen: set[str] = set()
        row_key = table_row_keys[table_key]
        for row in rows:
            record_id = str(row.get(row_key) or "").strip()
            if not record_id:
                raise CalculationBlocked(
                    f"源表 {table_key} 存在稳定记录标识为空的记录"
                )
            if record_id in seen:
                raise CalculationBlocked(
                    f"源表 {table_key} 的稳定记录标识重复：{record_id}"
                )
            seen.add(record_id)

    subject = dict(spec["subject"])
    subject_fields = {
        str(table_key): str(column_key)
        for table_key, column_key in subject.get("fields", {}).items()
    }
    subject_lookups = {
        str(table_key): dict(lookup)
        for table_key, lookup in subject.get("lookups", {}).items()
    }
    excluded = {
        str(value).strip()
        for value in subject.get("exclude_values", [])
        if str(value).strip()
    }
    exclude_empty = bool(subject.get("exclude_empty", True))
    subject_labels: dict[str, str] = {}
    resolved_subjects: dict[str, dict[str, str | None]] = {}
    mapping_failures: dict[
        tuple[str, str, str],
        dict[str, Any],
    ] = {}
    for table_key, rows in sources.items():
        if table_key not in table_definitions:
            continue
        resolved_subjects[table_key] = {}
        row_key = table_row_keys[table_key]
        for row in rows:
            record_id = str(row.get(row_key) or "").strip()
            value: str | None
            if table_key in subject_fields:
                value = str(row.get(subject_fields[table_key]) or "").strip()
                if (not value and exclude_empty) or value in excluded:
                    value = None
            elif table_key in subject_lookups:
                value, failure = _resolve_lookup_subject(
                    subject_lookups[table_key],
                    row,
                )
                if failure is not None:
                    failure_key = (
                        table_key,
                        failure["step_name"],
                        failure["match_label"],
                    )
                    collected = mapping_failures.setdefault(
                        failure_key,
                        {
                            "table": table_key,
                            "step_name": failure["step_name"],
                            "match_label": failure["match_label"],
                            "values": set(),
                            "source_rows": [],
                        },
                    )
                    collected["values"].add(failure["value"])
                    row_number = row.get("__row_number__")
                    if isinstance(row_number, int):
                        collected["source_rows"].append(
                            {"table": table_key, "row_number": row_number}
                        )
            else:
                value = None
            resolved_subjects[table_key][record_id] = value
            if value:
                subject_labels.setdefault(value, value)
    if bool(subject.get("include_total", False)):
        total_key = str(subject.get("total_key") or "__total__")
        subject_labels[total_key] = str(subject.get("total_label") or "整体")
    if not subject_labels and not mapping_failures:
        raise CalculationBlocked("没有找到可参与计算的核算对象")

    total_key = (
        str(subject.get("total_key") or "__total__")
        if bool(subject.get("include_total", False))
        else ""
    )
    metric_definitions = {
        str(metric["key"]): metric for metric in spec["metrics"]
    }
    results: list[PersonResult] = []
    errors: list[dict[str, Any]] = []
    for index, failure in enumerate(
        sorted(
            mapping_failures.values(),
            key=lambda item: (item["table"], item["step_name"]),
        ),
        start=1,
    ):
        values = sorted(str(value) for value in failure["values"])
        errors.append(
            {
                "person_id": (
                    "__subject_mapping__"
                    if len(mapping_failures) == 1
                    else f"__subject_mapping__:{index}"
                ),
                "subject_name": f"{subject.get('name') or '核算对象'}归属待处理",
                "code": "subject_mapping_error",
                "message": (
                    f"有{len(failure['source_rows'])}条记录无法通过"
                    f"“{failure['step_name']}”{failure['match_label']}："
                    f"{'、'.join(values[:10])}"
                    + ("等" if len(values) > 10 else "")
                ),
                "source_rows": failure["source_rows"][:200],
            }
        )
    ordered_subjects = sorted(
        subject_labels.items(),
        key=lambda item: (0 if item[0] == total_key else 1, item[1], item[0]),
    )
    for subject_id, display_name in ordered_subjects:
        metric_values: dict[str, Decimal] = {}
        metric_lineage: dict[str, dict[str, Any]] = {}
        try:
            for metric_key, metric in metric_definitions.items():
                source_table = str(metric["source_table"])
                metric_subject_scope = str(
                    metric.get("subject_scope") or "grouped"
                )
                if (
                    metric_subject_scope == "total_only"
                    and subject_id != total_key
                ):
                    continue
                row_key = table_row_keys[source_table]
                matched_rows: list[dict[str, Any]] = []
                for row in sources.get(source_table, []):
                    record_id = str(row.get(row_key) or "").strip()
                    row_subject = resolved_subjects.get(source_table, {}).get(
                        record_id
                    )
                    has_subject_resolver = (
                        source_table in subject_fields
                        or source_table in subject_lookups
                    )
                    if metric_subject_scope != "total_only":
                        if has_subject_resolver and row_subject is None:
                            continue
                        if (
                            has_subject_resolver
                            and subject_id != total_key
                            and row_subject != subject_id
                        ):
                            continue
                        if not has_subject_resolver and subject_id != total_key:
                            continue
                    where = metric.get("where")
                    if where is not None and not _evaluate_condition(
                        where,
                        row,
                        boundaries,
                    ):
                        continue
                    matched_rows.append(row)
                if str(metric["aggregate"]) == "count":
                    metric_value = Decimal(len(matched_rows))
                    value_fields: set[str] = set()
                else:
                    metric_value = Decimal(0)
                    value_fields = set()
                    for row in matched_rows:
                        value, fields_used = _evaluate(
                            metric["value"],
                            {source_table: row},
                            null_as_zero=bool(metric.get("null_as_zero", False)),
                        )
                        metric_value += value
                        value_fields.update(fields_used)
                if not metric_value.is_finite():
                    raise CalculationBlocked(
                        f"基础指标 {metric.get('name') or metric_key} 结果不是有限数值"
                    )
                condition_fields = _collect_fields(metric.get("where"))
                row_numbers = sorted(
                    {
                        int(row["__row_number__"])
                        for row in matched_rows
                        if isinstance(row.get("__row_number__"), int)
                    }
                )
                metric_values[metric_key] = metric_value
                metric_lineage[metric_key] = {
                    "metric_key": metric_key,
                    "metric_name": str(metric.get("name") or metric_key),
                    "source_table": source_table,
                    "aggregate": str(metric["aggregate"]),
                    "subject_scope": metric_subject_scope,
                    "null_as_zero": bool(metric.get("null_as_zero", False)),
                    "value": str(metric_value),
                    "fields": sorted(condition_fields.union(value_fields)),
                    "where": metric.get("where"),
                    "source_row_count": len(row_numbers),
                    "source_row_ranges": _row_ranges(source_table, row_numbers),
                    "source_rows": [
                        {"table": source_table, "row_number": row_number}
                        for row_number in row_numbers[:200]
                    ],
                    "source_rows_truncated": len(row_numbers) > 200,
                }

            values: dict[str, Decimal] = {}
            lineage: dict[str, dict[str, Any]] = {}
            result_values: dict[str, tuple[Decimal, set[str]]] = {}
            for output in spec["outputs"]:
                output_subject_scope = str(
                    output.get("subject_scope") or "grouped"
                )
                if (
                    output_subject_scope == "total_only"
                    and subject_id != total_key
                ):
                    continue
                value, metrics_used = _evaluate_metric_expression(
                    output["expression"],
                    metric_values,
                    results=result_values,
                    on_divide_by_zero=str(
                        output.get("on_divide_by_zero", "error")
                    ),
                )
                if not value.is_finite():
                    raise CalculationBlocked("计算结果不是有限数值")
                places = int(output.get("decimal_places", 2))
                quantizer = Decimal(1).scaleb(-places)
                rounded = value.quantize(
                    quantizer,
                    rounding=_ROUNDING[str(output.get("rounding", "half_up"))],
                )
                output_key = str(output["key"])
                values[output_key] = rounded
                result_values[output_key] = (value, set(metrics_used))
                used_lineage = [
                    metric_lineage[key] for key in sorted(metrics_used)
                ]
                source_rows = [
                    row
                    for detail in used_lineage
                    for row in detail["source_rows"]
                ]
                lineage[output_key] = {
                    "rule_version": rule_version,
                    "expression": output["expression"],
                    "on_divide_by_zero": str(
                        output.get("on_divide_by_zero", "error")
                    ),
                    "subject_scope": output_subject_scope,
                    "unit": str(output.get("unit") or ""),
                    "metrics": sorted(metrics_used),
                    "fields": sorted(
                        {
                            field
                            for detail in used_lineage
                            for field in detail["fields"]
                        }
                    ),
                    "source_rows": source_rows,
                    "source_row_count": sum(
                        int(detail["source_row_count"]) for detail in used_lineage
                    ),
                    "source_row_ranges": [
                        item
                        for detail in used_lineage
                        for item in detail["source_row_ranges"]
                    ],
                    "metric_details": used_lineage,
                }
            results.append(
                PersonResult(
                    subject_id,
                    values,
                    lineage,
                    display_name=display_name,
                )
            )
        except CalculationBlocked as exc:
            source_rows = [
                row
                for detail in metric_lineage.values()
                for row in detail.get("source_rows", [])
            ]
            errors.append(
                {
                    "person_id": subject_id,
                    "subject_name": display_name,
                    "code": "subject_calculation_error",
                    "message": str(exc),
                    "source_rows": source_rows,
                }
            )
        except (DecimalException, ArithmeticError, ValueError):
            errors.append(
                {
                    "person_id": subject_id,
                    "subject_name": display_name,
                    "code": "subject_calculation_error",
                    "message": "计算数值无效或超出可处理范围",
                    "source_rows": [],
                }
            )

    calculation_context = {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
    }
    input_hash = calculate_input_hash(
        spec,
        sources,
        calculation_context=calculation_context,
    )
    output_payload = {
        "results": [
            {
                "person_id": result.person_id,
                "display_name": result.display_name,
                "values": result.values,
                "lineage": result.lineage,
            }
            for result in results
        ],
        "errors": errors,
    }
    return CalculationResult(
        rule_version=rule_version,
        input_hash=input_hash,
        output_hash=_stable_hash(output_payload),
        results=tuple(results),
        errors=tuple(errors),
    )


def _calculation_context(
    period_start: date | str | None,
    period_end: date | str | None,
) -> dict[str, str]:
    if period_start is None and period_end is None:
        return {}
    return {
        "period_start": _coerce_period_date(
            period_start,
            "考核周期开始日期",
        ).isoformat(),
        "period_end": _coerce_period_date(
            period_end,
            "考核周期结束日期",
        ).isoformat(),
    }


def _coerce_period_date(value: date | str | None, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise CalculationBlocked(f"{label}缺失或格式无效")


def _date_boundaries(start: date, end: date) -> dict[str, date]:
    return {
        "period_start": start,
        "period_end": end,
        "month_start": end.replace(day=1),
        "month_end": end.replace(day=monthrange(end.year, end.month)[1]),
        "year_start": end.replace(month=1, day=1),
        "year_end": end.replace(month=12, day=31),
    }


def _resolve_lookup_subject(
    lookup: dict[str, Any],
    row: dict[str, Any],
) -> tuple[str | None, dict[str, str] | None]:
    resolution = resolve_lookup_path(lookup, row)
    return resolution.final_value, resolution.error


def resolve_lookup_path(
    lookup: dict[str, Any],
    row: dict[str, Any],
) -> LookupResolution:
    """Resolve one confirmed lookup chain and retain each audited step."""

    source_value = str(row.get(str(lookup["source_field"])) or "").strip()
    exclusions = {
        str(value).strip()
        for value in lookup.get("exclude_source_values") or []
    }
    if not source_value or source_value in exclusions:
        return LookupResolution(None)
    current = source_value
    resolved_steps: list[LookupStepResult] = []
    for step in lookup["steps"]:
        step_source = current
        separator = str(step.get("split_first") or "")
        if separator:
            current = current.split(separator, 1)[0]
        normalizers = list(step.get("normalizers") or [])
        normalized = _normalize_lookup_value(current, normalizers)
        mapping = {
            _normalize_lookup_value(str(key), normalizers): str(value).strip()
            for key, value in step["mapping"].items()
        }
        mapped, ambiguous = _lookup_mapping_value(
            normalized,
            mapping,
            list(step.get("match_strategies") or []),
        )
        if mapped is None:
            return LookupResolution(
                None,
                tuple(resolved_steps),
                {
                    "step_name": str(step["name"]),
                    "value": current.strip() or source_value,
                    "match_label": (
                        "唯一匹配"
                        if step.get("match_strategies")
                        else "精确匹配"
                    ),
                    "ambiguous": ambiguous,
                },
            )
        resolved_steps.append(
            LookupStepResult(
                step_name=str(step["name"]),
                source_value=step_source.strip() or source_value,
                mapped_value=mapped,
            )
        )
        current = mapped
    return LookupResolution(
        current.strip() or None,
        tuple(resolved_steps),
    )


def _lookup_mapping_value(
    source: str,
    mapping: dict[str, str],
    strategies: list[Any],
) -> tuple[str | None, bool]:
    exact = mapping.get(source)
    if exact is not None:
        return exact, False
    for strategy in strategies:
        if strategy == "exact":
            continue
        candidates: list[str] = []
        if strategy == "expand_branch_short_form":
            expanded, prefix = _expanded_branch_short_form(source)
            if expanded:
                exact = mapping.get(expanded)
                if exact is not None:
                    return exact, False
            if prefix:
                candidates = [
                    value
                    for key, value in mapping.items()
                    if key.startswith(prefix)
                ]
        elif strategy == "contains_unique":
            if len(source) >= 3:
                candidates = [
                    value
                    for key, value in mapping.items()
                    if source in key or key in source
                ]
        elif strategy == "strip_aftercare_suffix":
            simplified = source.replace("(善后)", "")
            if simplified != source:
                exact = mapping.get(simplified)
                if exact is not None:
                    return exact, False
                candidates = [
                    value
                    for key, value in mapping.items()
                    if simplified in key or key in simplified
                ]
        elif strategy == "strip_parenthetical":
            simplified = _without_parenthetical(source)
            if simplified and simplified != source:
                exact = mapping.get(simplified)
                if exact is not None:
                    return exact, False
                candidates = [
                    value
                    for key, value in mapping.items()
                    if simplified in key or key in simplified
                ]
        unique_values = sorted({value for value in candidates if value})
        if len(unique_values) == 1:
            return unique_values[0], False
        if len(unique_values) > 1:
            return None, True
    return None, False


def _expanded_branch_short_form(value: str) -> tuple[str, str]:
    marker = "分("
    marker_index = value.find(marker)
    if marker_index <= 0 or not value.endswith(")"):
        return "", ""
    prefix = f"{value[:marker_index]}分公司"
    return f"{prefix}{value[marker_index + 1 :]}", prefix


def _without_parenthetical(value: str) -> str:
    output: list[str] = []
    depth = 0
    for character in value:
        if character == "(":
            depth += 1
            continue
        if character == ")" and depth:
            depth -= 1
            continue
        if depth == 0:
            output.append(character)
    return "".join(output)


def _normalize_lookup_value(value: str, normalizers: list[Any]) -> str:
    normalized = str(value).strip()
    for normalizer in normalizers:
        if normalizer == "remove_whitespace":
            normalized = "".join(normalized.split())
        elif normalizer == "normalize_parentheses":
            normalized = (
                normalized.replace("（", "(")
                .replace("）", ")")
                .replace("【", "[")
                .replace("】", "]")
            )
        elif normalizer == "casefold":
            normalized = normalized.casefold()
    return normalized


def _evaluate_condition(
    condition: dict[str, Any],
    row: dict[str, Any],
    boundaries: dict[str, date],
) -> bool:
    if "all" in condition:
        return all(
            _evaluate_condition(item, row, boundaries)
            for item in condition["all"]
        )
    if "any" in condition:
        return any(
            _evaluate_condition(item, row, boundaries)
            for item in condition["any"]
        )
    op = str(condition["op"])
    left = _operand_value(condition["left"], row, boundaries)
    if op == "is_empty":
        return _is_empty(left)
    if op == "not_empty":
        return not _is_empty(left)
    right = _operand_value(condition["right"], row, boundaries)
    if op in {"in", "not_in"}:
        values = right if isinstance(right, list) else []
        contains = any(_values_equal(left, value) for value in values)
        return contains if op == "in" else not contains
    if op == "eq":
        return _values_equal(left, right)
    if op == "neq":
        return not _values_equal(left, right)
    if _is_empty(left) or _is_empty(right):
        return False
    left_value, right_value = _ordered_values(left, right)
    if op == "lt":
        return left_value < right_value
    if op == "lte":
        return left_value <= right_value
    if op == "gt":
        return left_value > right_value
    if op == "gte":
        return left_value >= right_value
    raise CalculationBlocked(f"筛选比较方式不受支持：{op}")


def _operand_value(
    operand: dict[str, Any],
    row: dict[str, Any],
    boundaries: dict[str, date],
) -> Any:
    if "field" in operand:
        _, column_key = str(operand["field"]).split(".", 1)
        return row.get(column_key)
    if "value" in operand:
        return operand["value"]
    if "values" in operand:
        return list(operand["values"])
    boundary = boundaries[str(operand["boundary"])]
    shift = operand.get("shift") or {}
    return _shift_date(
        boundary,
        years=int(shift.get("years", 0)),
        months=int(shift.get("months", 0)),
        days=int(shift.get("days", 0)),
    )


def _shift_date(
    value: date,
    *,
    years: int,
    months: int,
    days: int,
) -> date:
    month_index = value.year * 12 + (value.month - 1) + years * 12 + months
    shifted_year, shifted_month_index = divmod(month_index, 12)
    shifted_month = shifted_month_index + 1
    shifted_day = min(value.day, monthrange(shifted_year, shifted_month)[1])
    return date(shifted_year, shifted_month, shifted_day) + timedelta(days=days)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _values_equal(left: Any, right: Any) -> bool:
    if _is_empty(left) or _is_empty(right):
        return _is_empty(left) and _is_empty(right)
    try:
        left_value, right_value = _ordered_values(left, right)
    except CalculationBlocked:
        return str(left).strip() == str(right).strip()
    return left_value == right_value


def _ordered_values(left: Any, right: Any) -> tuple[Any, Any]:
    if isinstance(left, (date, datetime)) or isinstance(right, (date, datetime)):
        return _value_as_date(left), _value_as_date(right)
    if isinstance(left, (Decimal, int, float)) or isinstance(
        right,
        (Decimal, int, float),
    ):
        return _value_as_decimal(left), _value_as_decimal(right)
    return str(left).strip(), str(right).strip()


def _value_as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise CalculationBlocked(f"筛选日期格式无效：{value}") from None


def _value_as_decimal(value: Any) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CalculationBlocked(f"筛选数值格式无效：{value}") from None
    if not decimal.is_finite():
        raise CalculationBlocked("筛选数值不是有限数值")
    return decimal


def _evaluate_metric_expression(
    expression: dict[str, Any],
    metrics: dict[str, Decimal],
    *,
    results: dict[str, tuple[Decimal, set[str]]] | None = None,
    on_divide_by_zero: str = "error",
) -> tuple[Decimal, set[str]]:
    if "value" in expression:
        return Decimal(str(expression["value"])), set()
    if "metric" in expression:
        metric = str(expression["metric"])
        if metric not in metrics:
            raise CalculationBlocked(f"计算所需基础指标缺失：{metric}")
        return metrics[metric], {metric}
    if "result" in expression:
        result = str(expression["result"])
        if not results or result not in results:
            raise CalculationBlocked(f"计算所需前序结果缺失：{result}")
        value, used_metrics = results[result]
        return value, set(used_metrics)
    op = str(expression["op"])
    evaluated = [
        _evaluate_metric_expression(
            argument,
            metrics,
            results=results,
            on_divide_by_zero=on_divide_by_zero,
        )
        for argument in expression["args"]
    ]
    values = [item[0] for item in evaluated]
    used_metrics = set().union(*(item[1] for item in evaluated))
    if op == "add":
        return sum(values, Decimal(0)), used_metrics
    if op == "subtract":
        return values[0] - values[1], used_metrics
    if op == "multiply":
        result = Decimal(1)
        for value in values:
            result *= value
        return result, used_metrics
    if op == "divide":
        if values[1] == 0:
            if on_divide_by_zero == "zero":
                return Decimal(0), used_metrics
            raise CalculationBlocked("计算公式发生除零，当前指标不可比较")
        return values[0] / values[1], used_metrics
    if op == "min":
        return min(values), used_metrics
    if op == "max":
        return max(values), used_metrics
    if op == "abs":
        return abs(values[0]), used_metrics
    raise CalculationBlocked(f"不受支持的计算运算：{op}")


def _collect_fields(value: Any) -> set[str]:
    if isinstance(value, dict):
        fields = (
            {str(value["field"])}
            if "field" in value
            else set()
        )
        for child in value.values():
            fields.update(_collect_fields(child))
        return fields
    if isinstance(value, list):
        return set().union(*(_collect_fields(item) for item in value), set())
    return set()


def _row_ranges(table_key: str, row_numbers: list[int]) -> list[dict[str, Any]]:
    if not row_numbers:
        return []
    ranges: list[list[int]] = []
    start = previous = row_numbers[0]
    for row_number in row_numbers[1:]:
        if row_number == previous + 1:
            previous = row_number
            continue
        ranges.append([start, previous])
        start = previous = row_number
    ranges.append([start, previous])
    return [
        {
            "table": table_key,
            "ranges": ranges,
            "row_count": len(row_numbers),
        }
    ]


def _evaluate(
    expression: dict[str, Any],
    context: dict[str, dict[str, Any]],
    *,
    null_as_zero: bool = False,
) -> tuple[Decimal, set[str]]:
    if "value" in expression:
        return Decimal(str(expression["value"])), set()
    if "field" in expression:
        field = str(expression["field"])
        table_key, column_key = field.split(".", 1)
        if table_key not in context or column_key not in context[table_key]:
            raise CalculationBlocked(f"计算所需字段缺失：{field}")
        raw_value = context[table_key][column_key]
        if null_as_zero and (
            raw_value is None
            or (isinstance(raw_value, str) and not raw_value.strip())
        ):
            return Decimal(0), {field}
        try:
            value = Decimal(str(raw_value))
        except (InvalidOperation, ValueError):
            raise CalculationBlocked(f"计算字段不是合法数字：{field}") from None
        if not value.is_finite():
            raise CalculationBlocked(f"计算字段不是有限数值：{field}")
        return value, {field}
    op = str(expression["op"])
    evaluated = [
        _evaluate(
            argument,
            context,
            null_as_zero=null_as_zero,
        )
        for argument in expression["args"]
    ]
    values = [item[0] for item in evaluated]
    fields = set().union(*(item[1] for item in evaluated))
    if op == "add":
        return sum(values, Decimal(0)), fields
    if op == "subtract":
        return values[0] - values[1], fields
    if op == "multiply":
        result = Decimal(1)
        for value in values:
            result *= value
        return result, fields
    if op == "divide":
        if values[1] == 0:
            raise CalculationBlocked("计算公式发生除零")
        return values[0] / values[1], fields
    if op == "min":
        return min(values), fields
    if op == "max":
        return max(values), fields
    if op == "abs":
        return abs(values[0]), fields
    raise CalculationBlocked(f"不受支持的计算运算：{op}")
