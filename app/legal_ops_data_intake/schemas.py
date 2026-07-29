from __future__ import annotations

from typing import Any

from app.legal_ops_data_intake.workbook import TableColumn, TableSchema

CASE_MASTER_SCHEMA = TableSchema(
    key="case_master",
    name="案件主表",
    columns=(
        TableColumn("source_case_id", "ERP案件ID", "text", required=True, unique=True),
        TableColumn("case_name", "案件名称", "text", required=True),
        TableColumn("case_number", "案号", "text"),
        TableColumn("plaintiff", "原告", "text"),
        TableColumn("defendant", "被告", "text"),
        TableColumn("third_party", "第三人", "text"),
        TableColumn("our_litigation_position", "我方诉讼地位", "text"),
        TableColumn("owner_user_id", "承办法务员工编号", "text", required=True),
        TableColumn("team_id", "所属团队编号", "text", required=True),
        TableColumn("court", "法院", "text"),
        TableColumn("amount", "涉案金额", "decimal"),
        TableColumn("filing_date", "立案日期", "date"),
        TableColumn("status", "案件状态", "text", required=True),
        TableColumn("erp_updated_at", "ERP更新时间", "date"),
    ),
)


CASE_PROGRESS_SCHEMA = TableSchema(
    key="case_progress",
    name="案件进展表",
    columns=(
        TableColumn("source_case_id", "ERP案件ID", "text"),
        TableColumn("case_number", "案号", "text"),
        TableColumn("external_progress_id", "外部进展ID", "text", unique=True),
        TableColumn("progress_date", "进展日期", "date", required=True),
        TableColumn("progress_type", "进展类型", "text", required=True),
        TableColumn("content", "进展内容", "text", required=True),
        TableColumn("procedure_node", "程序节点", "text", required=True),
        TableColumn("next_plan", "下一步计划", "text"),
        TableColumn("plan_date", "计划日期", "date"),
        TableColumn("reporter_id", "录入人员工编号", "text", required=True),
        TableColumn("source_updated_at", "来源更新时间", "date"),
    ),
)


def performance_table_schema(
    table: dict[str, Any],
    *,
    row_key: str = "",
) -> TableSchema:
    columns: list[TableColumn] = []
    for item in table.get("columns") or []:
        data_type = str(item.get("type") or "text")
        if data_type not in {"text", "date", "decimal", "percentage", "integer"}:
            raise ValueError(
                f"规则表 {table.get('name') or table.get('key')} 包含不支持的数据类型"
            )
        columns.append(
            TableColumn(
                key=str(item["key"]),
                name=str(item.get("name") or item["key"]),
                data_type=data_type,  # type: ignore[arg-type]
                required=bool(item.get("required", False)),
                unique=bool(item.get("unique", False))
                or bool(row_key and str(item["key"]) == row_key),
            )
        )
    return TableSchema(
        key=str(table["key"]),
        name=str(table.get("name") or table["key"]),
        columns=tuple(columns),
        allow_extra_columns=bool(table.get("allow_extra_columns", False)),
    )


def schema_to_dict(schema: TableSchema) -> dict[str, Any]:
    return {
        "key": schema.key,
        "name": schema.name,
        "allow_extra_columns": schema.allow_extra_columns,
        "columns": [
            {
                "key": column.key,
                "name": column.name,
                "type": column.data_type,
                "required": column.required,
                "unique": column.unique,
            }
            for column in schema.columns
        ],
    }
