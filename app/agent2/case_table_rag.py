from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Sequence

from app.agent2.context_pack import KnowledgeEvidenceFrame
from app.agent2.fact_contracts import build_case_table_fact_contract
from app.agent2.fact_permissions import evaluate_case_fact_permission
from app.agent2.knowledge_resolver import KnowledgeQuery


DEFAULT_CASE_RAG_INDEX = Path("data/rag_indexes/case_tables/case_index.sqlite")


@dataclass(frozen=True)
class CaseTableDocument:
    doc_id: str
    source_type: str
    source_file: str
    sheet_name: str
    row_number: int
    table_type: str
    case_name: str = ""
    department: str = ""
    assignee_name: str = ""
    status: str = ""
    updated_at: str = ""
    text: str = ""
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaseLocationHint:
    case_name: str = ""
    court_or_location: str = ""
    department: str = ""
    assignee_name: str = ""
    source_id: str = ""


class CaseTableRagAdapter:
    source_type = "case_table_rag"

    def __init__(self, index_path: str | Path = DEFAULT_CASE_RAG_INDEX, *, limit: int = 5) -> None:
        self.index_path = Path(index_path)
        self.limit = max(1, int(limit or 5))

    def resolve(self, query: KnowledgeQuery) -> Sequence[KnowledgeEvidenceFrame]:
        if not self.index_path.exists():
            return ()
        if not _looks_like_case_table_query(query):
            return ()
        count_evidence = _case_count_evidence(self.index_path, query)
        if count_evidence is not None:
            return (_permission_checked_evidence(count_evidence, query=query),)
        documents = search_case_table_index(self.index_path, query.text, limit=self.limit)
        return tuple(_permission_checked_evidence(_document_evidence(document, query=query), query=query) for document in documents)


def write_case_table_index(
    documents: Iterable[CaseTableDocument | dict[str, Any]],
    *,
    sqlite_path: str | Path,
    jsonl_path: str | Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sqlite_target = Path(sqlite_path)
    sqlite_target.parent.mkdir(parents=True, exist_ok=True)
    if sqlite_target.exists():
        sqlite_target.unlink()

    normalized = [_case_document(document) for document in documents if _case_document(document).text]
    with sqlite3.connect(sqlite_target) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE documents (
                doc_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_file TEXT NOT NULL,
                sheet_name TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                table_type TEXT NOT NULL,
                case_name TEXT NOT NULL,
                department TEXT NOT NULL,
                assignee_name TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                text TEXT NOT NULL,
                facts_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE documents_fts USING fts5(
                doc_id UNINDEXED,
                case_name,
                department,
                assignee_name,
                text,
                tokenize='unicode61'
            )
            """
        )
        for document in normalized:
            conn.execute(
                """
                INSERT INTO documents (
                    doc_id, source_type, source_file, sheet_name, row_number, table_type,
                    case_name, department, assignee_name, status, updated_at, text, facts_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document.doc_id,
                    document.source_type,
                    document.source_file,
                    document.sheet_name,
                    document.row_number,
                    document.table_type,
                    document.case_name,
                    document.department,
                    document.assignee_name,
                    document.status,
                    document.updated_at,
                    document.text,
                    json.dumps(document.facts, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.execute(
                """
                INSERT INTO documents_fts (doc_id, case_name, department, assignee_name, text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    document.doc_id,
                    document.case_name,
                    document.department,
                    document.assignee_name,
                    document.text,
                ),
            )
        conn.commit()

    if jsonl_path is not None:
        jsonl_target = Path(jsonl_path)
        jsonl_target.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_target.open("w", encoding="utf-8") as handle:
            for document in normalized:
                handle.write(json.dumps(_document_payload(document), ensure_ascii=False, sort_keys=True) + "\n")

    summary = {
        "document_count": len(normalized),
        "sqlite_path": str(sqlite_target),
        "jsonl_path": str(jsonl_path or ""),
        **dict(metadata or {}),
    }
    meta_path = sqlite_target.with_name("case_index_meta.json")
    meta_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def search_case_table_index(index_path: str | Path, query_text: str, *, limit: int = 5) -> list[CaseTableDocument]:
    query = str(query_text or "").strip()
    if not query:
        return []
    target = Path(index_path)
    if not target.exists():
        return []

    with sqlite3.connect(target) as conn:
        conn.row_factory = sqlite3.Row
        rows = _search_fts(conn, query, limit=limit)
        if len(rows) < limit:
            existing_ids = {str(row["doc_id"]) for row in rows}
            rows.extend(
                row
                for row in _search_like(conn, query, limit=limit * 3)
                if str(row["doc_id"]) not in existing_ids
            )
        return [_document_from_row(row) for row in rows[:limit]]


def find_case_location_hint(
    index_path: str | Path,
    *,
    matter_hint: str = "",
    raw_text: str = "",
    limit: int = 5,
) -> CaseLocationHint | None:
    """Return a conservative court/location hint for a case candidate.

    This is used only for user-facing confirmation. It must not turn an inferred
    court into an authorized travel write by itself.
    """

    target = Path(index_path)
    if not target.exists():
        return None
    terms = _case_location_lookup_terms(matter_hint=matter_hint, raw_text=raw_text)
    if not terms:
        return None

    documents: list[CaseTableDocument] = []
    seen: set[str] = set()
    for term in terms:
        for document in search_case_table_index(target, term, limit=max(limit, 3)):
            key = document.doc_id or f"{document.table_type}:{document.sheet_name}:{document.row_number}"
            if key in seen:
                continue
            seen.add(key)
            documents.append(document)
            if len(documents) >= limit:
                break
        if len(documents) >= limit:
            break
    if not documents:
        return None

    for document in documents:
        location = _case_document_court_or_location(document)
        if location:
            return CaseLocationHint(
                case_name=document.case_name,
                court_or_location=location,
                department=document.department,
                assignee_name=document.assignee_name,
                source_id=document.doc_id,
            )
    first = documents[0]
    return CaseLocationHint(
        case_name=first.case_name,
        department=first.department,
        assignee_name=first.assignee_name,
        source_id=first.doc_id,
    )


def find_case_location_hint_by_identity(
    index_path: str | Path,
    *,
    case_number: str = "",
    case_name: str = "",
    source_id: str = "",
) -> CaseLocationHint | None:
    """Read a court/location only from the already-resolved Case row.

    Unlike :func:`find_case_location_hint`, this function never performs a
    fuzzy matter search.  A document must match the Case number, exact name,
    or exact source workbook coordinate before any location is returned.
    """

    target = Path(index_path)
    if not target.exists():
        return None
    normalized_number = str(case_number or "").strip()
    normalized_name = str(case_name or "").strip()
    source_file, source_sheet, source_row = _source_coordinate(source_id)
    if not any((normalized_number, normalized_name, source_row is not None)):
        return None

    with sqlite3.connect(target) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM documents ORDER BY row_number").fetchall()
    matched: list[CaseTableDocument] = []
    for row in rows:
        document = _document_from_row(row)
        facts = dict(document.facts or {})
        number_matches = bool(
            normalized_number
            and str(facts.get("案件编号") or "").strip() == normalized_number
        )
        name_matches = bool(
            normalized_name
            and (
                document.case_name.strip() == normalized_name
                or str(facts.get("案件名称") or "").strip() == normalized_name
            )
        )
        source_matches = bool(
            source_row is not None
            and document.row_number == source_row
            and (not source_sheet or document.sheet_name.strip() == source_sheet)
            and (
                not source_file
                or Path(document.source_file).name.casefold() == source_file.casefold()
            )
        )
        if source_matches or number_matches or name_matches:
            matched.append(document)
    if not matched:
        return None

    # Prefer the workbook coordinate, then a Case-number match, then exact
    # name.  Every path remains bound to the resolved Case identity.
    matched.sort(
        key=lambda document: (
            0
            if source_row is not None
            and document.row_number == source_row
            and (not source_sheet or document.sheet_name.strip() == source_sheet)
            else 1,
            0
            if normalized_number
            and str(document.facts.get("案件编号") or "").strip()
            == normalized_number
            else 1,
            0 if normalized_name and document.case_name.strip() == normalized_name else 1,
        )
    )
    document = matched[0]
    location = _case_document_court_or_location(document)
    return CaseLocationHint(
        case_name=document.case_name,
        court_or_location=location,
        department=document.department,
        assignee_name=document.assignee_name,
        source_id=document.doc_id,
    )


def _permission_checked_evidence(
    evidence: KnowledgeEvidenceFrame,
    *,
    query: KnowledgeQuery,
) -> KnowledgeEvidenceFrame:
    facts = dict(getattr(evidence, "facts", None) or {})
    decision = evaluate_case_fact_permission(query=query, facts=facts)
    if decision.allowed:
        _attach_permission_decision(facts, decision.as_dict())
        return replace(evidence, facts=facts)
    return KnowledgeEvidenceFrame(
        source_type="case_table_rag",
        source_id=f"permission_denied:{getattr(evidence, 'source_id', '')}",
        title="案件底表查询权限不足",
        summary="该案件底表问题超出当前用户可查询范围。",
        facts={
            "permission_denied": True,
            "permission": decision.as_dict(),
            "requested_by_user_id": query.user_id,
            "requested_by_dingtalk_user_id": query.dingtalk_user_id,
        },
        confidence=getattr(evidence, "confidence", 0.0),
        freshness=getattr(evidence, "freshness", ""),
    )


def _attach_permission_decision(facts: dict[str, Any], permission: dict[str, Any]) -> None:
    facts["permission"] = permission
    contract = facts.get("fact_contract")
    if isinstance(contract, dict):
        contract["permission"] = permission


def _case_count_evidence(index_path: str | Path, query: KnowledgeQuery) -> KnowledgeEvidenceFrame | None:
    spec = _case_count_query_spec(str(query.text or ""), metadata=query.metadata)
    if spec is None:
        return None
    target = Path(index_path)
    if not target.exists():
        return None
    if spec.get("metric_mode") == "defendant_monthly":
        with sqlite3.connect(target) as conn:
            conn.row_factory = sqlite3.Row
            rows = list(conn.execute("SELECT * FROM documents WHERE table_type = ?", ("defendant_case_table",)))
        return _defendant_monthly_evidence(rows, query=query, spec=spec)
    assignee_name = spec.get("assignee_name", "")
    department = spec.get("department", "")
    table_type = spec.get("table_type", "")
    status_filter = str(spec.get("status_filter") or "all")
    query_mode = str(spec.get("query_mode") or "count")
    group_by = str(spec.get("group_by") or "")
    clauses = []
    params: list[Any] = []
    if assignee_name:
        clauses.append("assignee_name LIKE ?")
        params.append(f"%{assignee_name}%")
    if department:
        clauses.append("department = ?")
        params.append(department)
    if table_type:
        clauses.append("table_type = ?")
        params.append(table_type)
    where_sql = " AND ".join(clauses) if clauses else "1=1"
    with sqlite3.connect(target) as conn:
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute(f"SELECT * FROM documents WHERE {where_sql}", params))
    selected_rows = [row for row in rows if _row_matches_status(row, status_filter)]
    if group_by == "department":
        return _department_group_evidence(
            selected_rows,
            query=query,
            table_type=table_type,
            status_filter=status_filter,
        )
    count = len(selected_rows)
    total_count = len(rows)
    sample_limit = 20 if query_mode == "list" else (10 if count <= 10 else 5)
    sample_rows = selected_rows[:sample_limit]
    table_label = _table_type_label(table_type)
    scope_label = department or assignee_name or "\u5168\u90e8"
    status_label = _status_filter_label(status_filter)
    title = f"{scope_label}{table_label}{status_label}\u6848\u4ef6\u6570\u91cf\u7edf\u8ba1"
    sample_names = [str(row["case_name"] or "") for row in sample_rows if str(row["case_name"] or "")]
    summary = f"\u6848\u4ef6\u5e95\u8868\u7edf\u8ba1\uff1a{scope_label}{table_label}{status_label}\u6848\u4ef6 {count} \u4ef6"
    if status_filter != "all":
        summary += f"\uff08\u8be5\u8303\u56f4\u603b\u8ba1 {total_count} \u4ef6\uff09"
    summary += "\u3002"
    if sample_names:
        summary += "\u6837\u4f8b\uff1a" + "\uff1b".join(sample_names[:3]) + "\u3002"
    return KnowledgeEvidenceFrame(
        source_type="case_table_rag",
        source_id=f"{query_mode}:{table_type or 'all'}:{department or assignee_name or 'all'}:{status_filter}",
        title=title,
        summary=summary,
        facts=_with_fact_contract({
            "assignee_name": assignee_name,
            "department": department,
            "table_type": table_type,
            "table_label": table_label,
            "case_count": count,
            "total_case_count": total_count,
            "status_filter": status_filter,
            "unclosed_only": status_filter == "unclosed",
            "query_mode": query_mode,
            "sample_limit": sample_limit,
            "sample_truncated": count > len(sample_names),
            "sample_case_names": sample_names,
            "requested_by_user_id": query.user_id,
            "requested_by_dingtalk_user_id": query.dingtalk_user_id,
        }, source_id=f"{query_mode}:{table_type or 'all'}:{department or assignee_name or 'all'}:{status_filter}", title=title, confidence=0.93, freshness="case_table_index"),
        confidence=0.93,
        freshness="case_table_index",
    )


def _department_group_evidence(
    rows: list[sqlite3.Row],
    *,
    query: KnowledgeQuery,
    table_type: str,
    status_filter: str,
) -> KnowledgeEvidenceFrame:
    groups: dict[str, int] = {}
    for row in rows:
        department = str(row["department"] or "").strip() or "\u672a\u6807\u6ce8\u90e8\u95e8"
        groups[department] = groups.get(department, 0) + 1
    ordered_groups = [
        {"department": department, "case_count": count}
        for department, count in sorted(groups.items(), key=lambda item: (-item[1], item[0]))
    ]
    table_label = _table_type_label(table_type)
    status_label = _status_filter_label(status_filter)
    top = "\uff1b".join(f"{item['department']} {item['case_count']}\u4ef6" for item in ordered_groups[:8])
    summary = f"\u6848\u4ef6\u5e95\u8868\u7edf\u8ba1\uff1a{table_label}{status_label}\u6848\u4ef6\u6309\u56e2\u961f\u5171 {len(rows)} \u4ef6\u3002"
    if top:
        summary += f"\u524d\u51e0\u9879\uff1a{top}\u3002"
    return KnowledgeEvidenceFrame(
        source_type="case_table_rag",
        source_id=f"group:department:{table_type or 'all'}:{status_filter}",
        title=f"{table_label}{status_label}\u6848\u4ef6\u56e2\u961f\u7edf\u8ba1",
        summary=summary,
        facts=_with_fact_contract({
            "table_type": table_type,
            "table_label": table_label,
            "status_filter": status_filter,
            "group_by": "department",
            "case_count": len(rows),
            "groups": ordered_groups,
            "requested_by_user_id": query.user_id,
            "requested_by_dingtalk_user_id": query.dingtalk_user_id,
        }, source_id=f"group:department:{table_type or 'all'}:{status_filter}", title=f"{table_label}{status_label}\u6848\u4ef6\u56e2\u961f\u7edf\u8ba1", confidence=0.94, freshness="case_table_index"),
        confidence=0.94,
        freshness="case_table_index",
    )


def _defendant_monthly_evidence(
    rows: list[sqlite3.Row],
    *,
    query: KnowledgeQuery,
    spec: dict[str, Any],
) -> KnowledgeEvidenceFrame:
    metric_kind = str(spec.get("metric_kind") or "inventory")
    cutoff = _defendant_stats_cutoff(rows)
    if cutoff is None:
        cutoff = _metadata_current_date(query.metadata) or date.today()
    scoped_rows = [row for row in rows if _row_matches_metric_scope(row, spec)]
    if spec.get("group_by") == "department":
        return _defendant_monthly_group_evidence(scoped_rows, query=query, spec=spec, cutoff=cutoff)

    period = _defendant_metric_period(spec, cutoff)
    stats = _defendant_monthly_stats(
        scoped_rows,
        cutoff,
        period_start=period["start"],
        period_end=period["end"],
        period_type=period["type"],
    )
    sample_rows = _defendant_metric_sample_rows(
        scoped_rows,
        cutoff,
        metric_kind=metric_kind,
        period_start=period["start"],
        period_end=period["end"],
    )
    scope_label = str(spec.get("department") or spec.get("assignee_name") or "\u5168\u90e8").strip()
    display_count = stats["inventory_count"] if metric_kind != "new" else stats["new_count"]
    period_label = period["label"]
    period_name = "\u5b63\u5ea6" if period["type"] == "quarter" else "\u6708\u5ea6"
    return KnowledgeEvidenceFrame(
        source_type="case_table_rag",
        source_id=f"defendant_monthly:{scope_label}:{metric_kind}:{period_label}",
        title=f"{scope_label}\u88ab\u544a\u6848\u4ef6{period_name}\u7edf\u8ba1",
        summary=(
            f"\u88ab\u544a\u6848\u4ef6{period_name}\u7edf\u8ba1\uff1a{scope_label}"
            f"\u622a\u81f3{period['end'].isoformat()}\u5b58\u91cf{stats['inventory_count']}\u4ef6\uff0c"
            f"{period_label}\u65b0\u589e{stats['new_count']}\u4ef6\u3002"
        ),
        facts=_with_fact_contract({
            "metric_mode": "defendant_monthly",
            "metric_kind": metric_kind,
            "period_type": period["type"],
            "period_start": period["start"].isoformat(),
            "period_end": period["end"].isoformat(),
            "department": str(spec.get("department") or ""),
            "assignee_name": str(spec.get("assignee_name") or ""),
            "table_type": "defendant_case_table",
            "table_label": "\u88ab\u544a",
            "case_count": display_count,
            "as_of_date": period["end"].isoformat(),
            "period_label": period_label,
            "scope_case_count": len(scoped_rows),
            "sample_case_names": [str(row["case_name"] or "") for row in sample_rows if str(row["case_name"] or "")],
            "sample_truncated": display_count > len(sample_rows),
            "requested_by_user_id": query.user_id,
            "requested_by_dingtalk_user_id": query.dingtalk_user_id,
            **stats,
        }, source_id=f"defendant_monthly:{scope_label}:{metric_kind}:{period_label}", title=f"{scope_label}\u88ab\u544a\u6848\u4ef6{period_name}\u7edf\u8ba1", confidence=0.95, freshness="case_table_index"),
        confidence=0.95,
        freshness="case_table_index",
    )


def _defendant_monthly_group_evidence(
    rows: list[sqlite3.Row],
    *,
    query: KnowledgeQuery,
    spec: dict[str, Any],
    cutoff: date,
) -> KnowledgeEvidenceFrame:
    metric_kind = str(spec.get("metric_kind") or "inventory")
    period = _defendant_metric_period(spec, cutoff)
    by_department: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        department = str(row["department"] or "").strip() or "\u672a\u6807\u6ce8\u90e8\u95e8"
        by_department.setdefault(department, []).append(row)
    groups = []
    for department, department_rows in by_department.items():
        stats = _defendant_monthly_stats(
            department_rows,
            cutoff,
            period_start=period["start"],
            period_end=period["end"],
            period_type=period["type"],
        )
        sort_count = stats["new_count"] if metric_kind == "new" else stats["inventory_count"]
        groups.append({"department": department, "case_count": sort_count, **stats})
    groups.sort(key=lambda item: (-int(item.get("case_count") or 0), str(item.get("department") or "")))
    period_label = period["label"]
    period_name = "\u5b63\u5ea6" if period["type"] == "quarter" else "\u6708\u5ea6"
    return KnowledgeEvidenceFrame(
        source_type="case_table_rag",
        source_id=f"defendant_monthly_group:{metric_kind}:{period_label}",
        title=f"\u88ab\u544a\u6848\u4ef6{_defendant_metric_label(metric_kind)}\u56e2\u961f\u7edf\u8ba1",
        summary=(
            f"\u88ab\u544a\u6848\u4ef6{period_name}\u7edf\u8ba1\uff1a\u622a\u81f3{period['end'].isoformat()}"
            f"\u6309\u56e2\u961f\u5171{len(rows)}\u6761\u5e95\u8868\u8bb0\u5f55\u3002"
        ),
        facts=_with_fact_contract({
            "metric_mode": "defendant_monthly",
            "metric_kind": metric_kind,
            "period_type": period["type"],
            "period_start": period["start"].isoformat(),
            "period_end": period["end"].isoformat(),
            "group_by": "department",
            "table_type": "defendant_case_table",
            "table_label": "\u88ab\u544a",
            "case_count": sum(int(group.get("case_count") or 0) for group in groups),
            "as_of_date": period["end"].isoformat(),
            "period_label": period_label,
            "groups": groups,
            "requested_by_user_id": query.user_id,
            "requested_by_dingtalk_user_id": query.dingtalk_user_id,
        }, source_id=f"defendant_monthly_group:{metric_kind}:{period_label}", title=f"\u88ab\u544a\u6848\u4ef6{_defendant_metric_label(metric_kind)}\u56e2\u961f\u7edf\u8ba1", confidence=0.95, freshness="case_table_index"),
        confidence=0.95,
        freshness="case_table_index",
    )


def _search_fts(conn: sqlite3.Connection, query: str, *, limit: int) -> list[sqlite3.Row]:
    terms = _fts_terms(query)
    if not terms:
        return []
    try:
        return list(
            conn.execute(
                """
                SELECT d.*
                FROM documents_fts f
                JOIN documents d ON d.doc_id = f.doc_id
                WHERE documents_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (" OR ".join(terms), limit),
            )
        )
    except sqlite3.Error:
        return []


def _search_like(conn: sqlite3.Connection, query: str, *, limit: int) -> list[sqlite3.Row]:
    needles = _like_terms(query)
    if not needles:
        needles = [query]
    clauses: list[str] = []
    params: list[Any] = []
    for term in needles[:8]:
        value = f"%{term}%"
        clauses.append("(text LIKE ? OR case_name LIKE ? OR department LIKE ? OR assignee_name LIKE ?)")
        params.extend([value, value, value, value])
    sql = f"SELECT * FROM documents WHERE {' OR '.join(clauses)} LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


def _case_location_lookup_terms(*, matter_hint: str, raw_text: str) -> list[str]:
    values = [str(matter_hint or ""), str(raw_text or "")]
    terms: list[str] = []
    for value in values:
        for term in _case_lookup_terms_from_text(value):
            if term not in terms:
                terms.append(term)
    return terms


def _case_lookup_terms_from_text(text: str) -> list[str]:
    value = str(text or "")
    if not value:
        return []
    cleaned = re.sub(
        r"(?:今天|今日|明天|明日|后天|下周[一二三四五六日天]?|本周[一二三四五六日天]?|去|赴|到|前往|开庭|庭审|出庭|沟通|处理|调解|案件|案)",
        " ",
        value,
    )
    candidates: list[str] = []
    for match in re.finditer(r"([\u4e00-\u9fa5A-Za-z0-9]{2,24}?)(?:案件|案)(?!例|情|由|卷|外)", value):
        candidates.append(match.group(1))
    for token in re.split(r"[\s，,。；;、：:（）()]+", cleaned):
        token = token.strip()
        if _valid_case_lookup_term(token):
            candidates.append(token)
    result: list[str] = []
    for candidate in candidates:
        term = _clean_case_lookup_term(candidate)
        if _valid_case_lookup_term(term) and term not in result:
            result.append(term)
    return result[:4]


def _clean_case_lookup_term(value: str) -> str:
    term = str(value or "").strip(" ：:，,。；;、的")
    term = re.sub(r"^(?:预计|估计|可能|准备|计划|需要|要)", "", term)
    term = re.sub(r"(?:案件|案|开庭|庭审|出庭)$", "", term)
    return term.strip(" ：:，,。；;、的")


def _valid_case_lookup_term(value: str) -> bool:
    term = str(value or "").strip()
    if len(term) < 2 or len(term) > 24:
        return False
    if term in {"案件", "开庭", "庭审", "出庭", "法院", "明天", "后天", "下周", "今天"}:
        return False
    if _contains_any(term, ("\u65b9\u6848", "\u6863\u6848", "\u6848\u4f8b", "\u7b54\u6848")):
        return False
    return bool(re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", term))


def _case_document_court_or_location(document: CaseTableDocument) -> str:
    facts = dict(document.facts or {})
    key_priority = (
        "承办法院",
        "受理法院",
        "执行法院",
        "一审法院",
        "二审法院",
        "管辖法院",
        "法院名称",
        "开庭法院",
        "开庭地点",
        "庭审地点",
        "仲裁委",
        "仲裁委员会",
        "受理_机构名称",
        "受理机构名称",
    )
    for key in key_priority:
        value = _clean_court_or_location_value(facts.get(key))
        if value:
            return value
    for key, value in facts.items():
        key_text = str(key or "")
        if not _looks_like_location_fact_key(key_text):
            continue
        location = _clean_court_or_location_value(value)
        if location:
            return location
    return _extract_location_from_document_text(document.text)


def _looks_like_location_fact_key(key: str) -> bool:
    value = str(key or "")
    if not _contains_any(value, ("\u6cd5\u9662", "\u6cd5\u5ead", "\u4ef2\u88c1\u59d4", "\u5f00\u5ead\u5730\u70b9", "\u5ead\u5ba1\u5730\u70b9")):
        return False
    return not _contains_any(value, ("\u662f\u5426", "\u53cd\u9988", "\u6e05\u5355", "\u4fdd\u5168", "\u91d1\u989d", "\u94f6\u884c", "\u8d26\u53f7"))


def _clean_court_or_location_value(value: Any) -> str:
    text = str(value or "").strip(" ：:，,。；;、")
    if not text or text in {"/", "-", "无", "暂无", "否", "是", "0"}:
        return ""
    if len(text) > 40:
        return ""
    if _contains_any(text, ("\u662f\u5426", "\u53cd\u9988", "\u6e05\u5355", "\u91d1\u989d")):
        return ""
    if not _contains_any(text, ("\u6cd5\u9662", "\u6cd5\u5ead", "\u4ef2\u88c1", "\u5e02", "\u533a", "\u53bf", "\u5dde", "\u9662")):
        return ""
    return text


def _extract_location_from_document_text(text: str) -> str:
    value = str(text or "")
    for pattern in (
        r"(?:承办法院|受理法院|执行法院|一审法院|二审法院|管辖法院|法院名称|开庭法院|开庭地点|庭审地点|仲裁委|仲裁委员会)[:：]\s*([^；;，,\n]{2,40})",
        r"((?:[\u4e00-\u9fa5]{2,20}(?:人民法院|仲裁委员会|中级人民法院|高级人民法院)))",
    ):
        match = re.search(pattern, value)
        if not match:
            continue
        location = _clean_court_or_location_value(match.group(1))
        if location:
            return location
    return ""


def _source_coordinate(source_id: str) -> tuple[str, str, int | None]:
    value = str(source_id or "").strip()
    if "#" not in value or "!" not in value:
        return "", "", None
    source_file, coordinate = value.rsplit("#", 1)
    sheet, row_text = coordinate.rsplit("!", 1)
    try:
        row_number = int(row_text.strip())
    except (TypeError, ValueError):
        return Path(source_file.strip()).name, sheet.strip(), None
    return Path(source_file.strip()).name, sheet.strip(), row_number


def _case_document(value: CaseTableDocument | dict[str, Any]) -> CaseTableDocument:
    if isinstance(value, CaseTableDocument):
        return value
    return CaseTableDocument(
        doc_id=str(value.get("doc_id") or ""),
        source_type=str(value.get("source_type") or "case_table_rag"),
        source_file=str(value.get("source_file") or ""),
        sheet_name=str(value.get("sheet_name") or ""),
        row_number=int(value.get("row_number") or 0),
        table_type=str(value.get("table_type") or ""),
        case_name=str(value.get("case_name") or ""),
        department=str(value.get("department") or ""),
        assignee_name=str(value.get("assignee_name") or ""),
        status=str(value.get("status") or ""),
        updated_at=str(value.get("updated_at") or ""),
        text=str(value.get("text") or ""),
        facts=dict(value.get("facts") or {}),
    )


def _document_from_row(row: sqlite3.Row) -> CaseTableDocument:
    try:
        facts = json.loads(str(row["facts_json"] or "{}"))
    except json.JSONDecodeError:
        facts = {}
    return CaseTableDocument(
        doc_id=str(row["doc_id"] or ""),
        source_type=str(row["source_type"] or "case_table_rag"),
        source_file=str(row["source_file"] or ""),
        sheet_name=str(row["sheet_name"] or ""),
        row_number=int(row["row_number"] or 0),
        table_type=str(row["table_type"] or ""),
        case_name=str(row["case_name"] or ""),
        department=str(row["department"] or ""),
        assignee_name=str(row["assignee_name"] or ""),
        status=str(row["status"] or ""),
        updated_at=str(row["updated_at"] or ""),
        text=str(row["text"] or ""),
        facts=facts,
    )


def _document_evidence(document: CaseTableDocument, *, query: KnowledgeQuery) -> KnowledgeEvidenceFrame:
    title = document.case_name or f"{document.table_type} row {document.row_number}"
    source_id = f"{document.table_type}:{document.sheet_name}:{document.row_number}:{document.doc_id}"
    summary_parts = [
        f"案件表命中：{title}",
        f"来源 {document.source_file} / {document.sheet_name} 第 {document.row_number} 行",
    ]
    if document.department:
        summary_parts.append(f"法务部门：{document.department}")
    if document.assignee_name:
        summary_parts.append(f"负责人：{document.assignee_name}")
    facts = {
        **document.facts,
        "doc_id": document.doc_id,
        "table_type": document.table_type,
        "case_name": document.case_name,
        "department": document.department,
        "assignee_name": document.assignee_name,
        "status": document.status,
        "source_file": document.source_file,
        "sheet_name": document.sheet_name,
        "row_number": document.row_number,
        "requested_by_user_id": query.user_id,
        "requested_by_dingtalk_user_id": query.dingtalk_user_id,
    }
    return KnowledgeEvidenceFrame(
        source_type="case_table_rag",
        source_id=source_id,
        title=title,
        summary="；".join(summary_parts) + "。",
        facts=_with_fact_contract(
            facts,
            source_id=source_id,
            title=title,
            confidence=0.86,
            freshness=document.updated_at or "case_table_index",
        ),
        confidence=0.86,
        freshness=document.updated_at or "case_table_index",
    )


def _document_payload(document: CaseTableDocument) -> dict[str, Any]:
    return {
        "doc_id": document.doc_id,
        "source_type": document.source_type,
        "source_file": document.source_file,
        "sheet_name": document.sheet_name,
        "row_number": document.row_number,
        "table_type": document.table_type,
        "case_name": document.case_name,
        "department": document.department,
        "assignee_name": document.assignee_name,
        "status": document.status,
        "updated_at": document.updated_at,
        "text": document.text,
        "facts": document.facts,
    }


def _with_fact_contract(
    facts: dict[str, Any],
    *,
    source_id: str,
    title: str,
    confidence: float,
    freshness: str,
) -> dict[str, Any]:
    payload = dict(facts)
    payload["fact_contract"] = build_case_table_fact_contract(
        source_id=source_id,
        title=title,
        facts=payload,
        confidence=confidence,
        freshness=freshness,
    )
    return payload


def _looks_like_case_table_query(query: KnowledgeQuery) -> bool:
    if "case_table_rag" in set(query.source_types):
        return True
    text = str(query.text or "")
    if _case_count_query_spec(text, metadata=query.metadata) is not None:
        return True
    if query.intent in {"case_query", "internal_qa"} and "案" in text:
        return True
    return "案" in text and any(
        marker in text
        for marker in (
            "案件",
            "案号",
            "原告",
            "被告",
            "诉讼",
            "仲裁",
            "执行",
            "开庭",
            "承办",
            "负责人",
            "进展",
            "底表",
            "台账",
        )
    )


_CASE_ALL_SCOPE_MARKERS = (
    "全部案件",
    "所有案件",
    "全量案件",
    "全部",
    "所有",
    "总体",
    "整体",
    "总量",
    "总数",
    "总共",
    "全中心",
    "法务中心",
    "全条线",
)

_CASE_REQUEST_VERB_MARKERS = (
    "发我",
    "发给我",
    "给我发",
    "给我",
    "发一下",
    "发下",
    "看一下",
    "看下",
    "查一下",
    "查一查",
    "查下",
    "帮我",
    "帮忙",
    "统计一下",
    "统计下",
    "拉一下",
    "列一下",
)


def _defendant_metric_period_spec(text: str, *, metadata: dict[str, Any] | None = None) -> dict[str, str]:
    value = str(text or "")
    quarter = _quarter_number_from_text(value)
    if quarter is None:
        return {}
    base_date = _metadata_current_date(metadata) or date.today()
    year = _explicit_year_from_text(value) or base_date.year
    if _contains_any(value, ("\u53bb\u5e74", "\u4e0a\u5e74", "\u4e0a\u4e00\u5e74")):
        year -= 1
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    period_start = date(year, start_month, 1)
    period_end = date(year, end_month, _last_day_of_month(year, end_month))
    return {
        "period_type": "quarter",
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "period_label": f"{year}-Q{quarter}",
    }


def _quarter_number_from_text(text: str) -> int | None:
    value = str(text or "")
    match = re.search(r"(?:第)?([一二三四1234])\s*季度", value)
    if not match:
        match = re.search(r"[Qq]\s*([1-4])", value)
    if not match:
        return None
    token = match.group(1)
    return {"一": 1, "二": 2, "三": 3, "四": 4}.get(token, int(token) if token.isdigit() else 0) or None


def _explicit_year_from_text(text: str) -> int | None:
    match = re.search(r"(20\d{2})\s*年?", str(text or ""))
    return int(match.group(1)) if match else None


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def _strip_metric_period_words(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"(?:20\d{2}\s*年?)?(?:第)?[一二三四1234]\s*季度", " ", value)
    value = re.sub(r"(?:20\d{2}\s*年?)?[Qq]\s*[1-4]", " ", value)
    for marker in (
        "\u672c\u5b63\u5ea6",
        "\u5f53\u5b63\u5ea6",
        "\u8fd9\u4e2a\u5b63\u5ea6",
        "\u5b63\u5ea6",
        "\u6708\u5ea6",
        "\u6570\u636e",
        "\u540c\u6bd4\u6570\u636e",
        "\u73af\u6bd4\u6570\u636e",
        "\u4eca\u5e74",
        "\u53bb\u5e74",
        "\u4e0a\u5e74",
        "\u540c\u671f",
    ):
        value = value.replace(marker, " ")
    return value


def _is_metric_period_text(value: str) -> bool:
    text = str(value or "")
    return _contains_any(text, ("\u5b63\u5ea6", "\u6708\u5ea6", "\u6570\u636e", "\u540c\u6bd4", "\u73af\u6bd4"))


def _case_count_query_spec(text: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
    value = str(text or "").strip()
    if not value:
        return None
    recent_spec = _recent_case_count_spec(metadata)
    status_filter = _case_status_filter(value)
    all_cases = _asks_all_cases(value)
    group_by = "department" if _asks_group_by_department(value) else ""
    department = _department_from_query(value)
    query_mode = "list" if _asks_case_list(value) else "count"
    period_spec = _defendant_metric_period_spec(value, metadata=metadata)
    followup = _looks_like_case_context_followup(value, recent_spec=recent_spec)
    has_query_marker = _contains_any(
        value,
        (
            "\u6709\u591a\u5c11",
            "\u591a\u5c11",
            "\u51e0\u4e2a",
            "\u51e0\u4ef6",
            "\u51e0\u6761",
            "\u7edf\u8ba1",
            "\u6570\u91cf",
            "\u6570\u636e",
            "\u60c5\u51b5",
            "\u54ea\u4e9b",
            "\u54ea\u51e0\u4ef6",
            "\u5217\u51fa",
            "\u660e\u7ec6",
            "\u540d\u5355",
            *_CASE_REQUEST_VERB_MARKERS,
        ),
    )
    metric_mode = "defendant_monthly" if _looks_like_defendant_monthly_metric_query(value, recent_spec=recent_spec) else ""
    metric_kind = _defendant_monthly_metric_kind(value, recent_spec=recent_spec) if metric_mode else ""
    has_quantitative_marker = _contains_any(
        value,
        (
            "\u6709\u591a\u5c11",
            "\u591a\u5c11",
            "\u51e0\u4e2a",
            "\u51e0\u4ef6",
            "\u51e0\u6761",
            "\u7edf\u8ba1",
            "\u6570\u91cf",
            "\u6570\u636e",
            "\u54ea\u4e9b",
            "\u54ea\u51e0\u4ef6",
            "\u5217\u51fa",
            "\u660e\u7ec6",
            "\u540d\u5355",
        ),
    )
    if _contains_any(value, ("\u8fdb\u5c55", "\u5e95\u8868", "\u53f0\u8d26")) and not has_quantitative_marker and not followup and not metric_mode:
        return None
    if not has_query_marker and not followup and not metric_mode:
        return None
    if not _contains_any(value, ("\u6848", "\u6848\u4ef6", "\u539f\u544a", "\u88ab\u544a", "\u5b58\u91cf", "\u65b0\u589e")) and not (
        status_filter != "all" and recent_spec is not None
    ) and not followup and not metric_mode:
        return None
    assignee_name = "" if department or group_by or all_cases else _assignee_name_from_count_query(value)
    if metric_mode and _is_metric_period_text(assignee_name):
        assignee_name = ""
    if not assignee_name and recent_spec is not None and not all_cases:
        assignee_name = str(recent_spec.get("assignee_name") or "")
    if not department and recent_spec is not None and not assignee_name and not all_cases:
        department = str(recent_spec.get("department") or "")
    table_type = ""
    if "\u88ab\u544a" in value:
        table_type = "defendant_case_table"
    elif "\u539f\u544a" in value:
        table_type = "plaintiff_case_table"
    elif recent_spec is not None and not all_cases:
        table_type = str(recent_spec.get("table_type") or "")
    if metric_mode:
        table_type = "defendant_case_table"
    if metric_mode and query_mode == "count" and _asks_case_list(value):
        query_mode = "list"
    if metric_mode and not assignee_name and not department and not group_by:
        all_cases = True
    if not assignee_name and not department and not group_by and not all_cases:
        return None
    return {
        "assignee_name": assignee_name,
        "department": department,
        "table_type": table_type,
        "status_filter": status_filter,
        "unclosed_only": status_filter == "unclosed",
        "query_mode": query_mode,
        "group_by": group_by,
        "metric_mode": metric_mode,
        "metric_kind": metric_kind,
        **period_spec,
    }


def _assignee_name_from_count_query(text: str) -> str:
    value = _strip_case_query_modifiers(str(text or ""))
    for marker in (
        "\u88ab\u544a\u6848\u4ef6",
        "\u539f\u544a\u6848\u4ef6",
        "\u88ab\u544a\u6848",
        "\u539f\u544a\u6848",
        "\u6848\u4ef6",
        "\u6848",
        "\u540d\u4e0b",
        "\u624b\u91cc",
        "\u8d1f\u8d23",
    ):
        if marker in value:
            name = _last_name_token(value.split(marker, 1)[0])
            if _is_likely_assignee_name(name):
                return name
    cleaned = value
    for marker in (
        *_CASE_REQUEST_VERB_MARKERS,
        *_CASE_ALL_SCOPE_MARKERS,
        "\u67e5\u4e00\u4e0b",
        "\u67e5\u4e0b",
        "\u770b\u4e0b",
        "\u5e2e\u6211",
        "\u7edf\u8ba1",
        "\u76ee\u524d",
        "\u5f53\u524d",
        "\u73b0\u5728",
        "\u5b58\u91cf",
        "\u65b0\u589e",
        "\u4e0b\u964d\u7387",
        "\u540c\u6bd4",
        "\u73af\u6bd4",
        "\u88ab\u544a",
        "\u539f\u544a",
        "\u6848\u4ef6",
        "\u6848",
        "\u6709\u591a\u5c11",
        "\u591a\u5c11",
        "\u51e0\u4e2a",
        "\u51e0\u4ef6",
        "\u51e0\u6761",
        "\u7684",
    ):
        cleaned = cleaned.replace(marker, " ")
    name = _last_name_token(cleaned)
    return name if _is_likely_assignee_name(name) else ""


def _strip_case_query_modifiers(text: str) -> str:
    value = _strip_metric_period_words(str(text or ""))
    for marker in (
        *_CASE_REQUEST_VERB_MARKERS,
        *_CASE_ALL_SCOPE_MARKERS,
        "\u672a\u7ed3\u6848",
        "\u6ca1\u7ed3\u6848",
        "\u5c1a\u672a\u7ed3\u6848",
        "\u672a\u5b8c\u7ed3",
        "\u672a\u5173\u95ed",
        "\u5728\u529e",
        "\u672a\u529e\u7ed3",
        "\u76ee\u524d",
        "\u5f53\u524d",
        "\u73b0\u5728",
        "\u5b58\u91cf",
        "\u65b0\u589e",
        "\u4e0b\u964d\u7387",
        "\u540c\u6bd4",
        "\u73af\u6bd4",
        "\u5df2\u7ed3\u6848",
        "\u5df2\u5b8c\u7ed3",
        "\u5df2\u5173\u95ed",
        "\u5df2\u529e\u7ed3",
        "\u54ea\u4e9b",
        "\u54ea\u51e0\u4ef6",
        "\u5177\u4f53",
        "\u5217\u51fa",
        "\u5217\u4e00\u4e0b",
        "\u660e\u7ec6",
        "\u540d\u5355",
        "\u6e05\u5355",
        "\u6570\u636e",
        "\u6708\u5ea6",
        "\u5b63\u5ea6",
        "\u81ea\u7136\u6708",
        "\u81ea\u7136\u5b63\u5ea6",
        "\u7684",
    ):
        value = value.replace(marker, "")
    return value


def _asks_unclosed_cases(text: str) -> bool:
    value = str(text or "")
    return _contains_any(
        value,
        (
            "\u672a\u7ed3\u6848",
            "\u6ca1\u7ed3\u6848",
            "\u5c1a\u672a\u7ed3\u6848",
            "\u672a\u5b8c\u7ed3",
            "\u672a\u5173\u95ed",
            "\u5728\u529e",
            "\u672a\u529e\u7ed3",
        ),
    )


def _asks_closed_cases(text: str) -> bool:
    value = str(text or "")
    return _contains_any(
        value,
        (
            "\u5df2\u7ed3\u6848",
            "\u5df2\u5b8c\u7ed3",
            "\u5df2\u5173\u95ed",
            "\u5df2\u529e\u7ed3",
            "\u7ed3\u6848\u7684",
        ),
    )


def _case_status_filter(text: str) -> str:
    if _asks_unclosed_cases(text):
        return "unclosed"
    if _asks_closed_cases(text):
        return "closed"
    return "all"


def _asks_case_list(text: str) -> bool:
    return _contains_any(
        str(text or ""),
        (
            "\u54ea\u4e9b",
            "\u54ea\u51e0\u4ef6",
            "\u5177\u4f53",
            "\u5217\u51fa",
            "\u5217\u4e00\u4e0b",
            "\u660e\u7ec6",
            "\u540d\u5355",
            "\u6e05\u5355",
        ),
    )


def _asks_group_by_department(text: str) -> bool:
    value = str(text or "")
    return _contains_any(value, ("\u5404\u56e2\u961f", "\u6bcf\u4e2a\u56e2\u961f", "\u6309\u56e2\u961f", "\u56e2\u961f\u7edf\u8ba1", "\u5404\u90e8\u95e8", "\u6309\u90e8\u95e8"))


def _department_from_query(text: str) -> str:
    value = str(text or "")
    departments = (
        "\u7efc\u5408\u7ba1\u7406\u90e8",
        "\u6cd5\u52a1\u4e00\u90e8",
        "\u6cd5\u52a1\u4e8c\u90e8",
        "\u6cd5\u52a1\u4e09\u90e8",
        "\u6cd5\u52a1\u56db\u90e8",
        "\u6cd5\u52a1\u4e94\u90e8",
        "\u6cd5\u52a1\u516d\u90e8",
        "\u6cd5\u52a1\u4e03\u90e8",
        "\u6cd5\u52a1\u516b\u90e8",
        "\u6cd5\u52a1\u4e5d\u90e8",
        "\u6cd5\u52a1\u5341\u90e8",
        "\u6d77\u5916\u6cd5\u52a1\u90e8",
        "\u7f8e\u745e\u5fb7\u6cd5\u52a1\u90e8",
    )
    for department in departments:
        if department in value:
            return department
    shorthand = {
        "\u4e00\u90e8": "\u6cd5\u52a1\u4e00\u90e8",
        "\u4e8c\u90e8": "\u6cd5\u52a1\u4e8c\u90e8",
        "\u4e09\u90e8": "\u6cd5\u52a1\u4e09\u90e8",
        "\u56db\u90e8": "\u6cd5\u52a1\u56db\u90e8",
        "\u4e94\u90e8": "\u6cd5\u52a1\u4e94\u90e8",
        "\u516d\u90e8": "\u6cd5\u52a1\u516d\u90e8",
    }
    if "\u88ab\u544a" in value or "\u6848" in value:
        for marker, department in shorthand.items():
            if marker in value:
                return department
    compact = re.sub(r"\s+", "", value)
    if "\u5462" in compact and len(compact) <= 8:
        for marker, department in shorthand.items():
            if marker in compact and f"{marker}\u5206" not in compact:
                return department
    return ""


def _asks_all_cases(text: str) -> bool:
    return _contains_any(str(text or ""), _CASE_ALL_SCOPE_MARKERS)


def _looks_like_defendant_monthly_metric_query(text: str, *, recent_spec: dict[str, Any] | None = None) -> bool:
    value = str(text or "")
    if recent_spec is not None and recent_spec.get("metric_mode") == "defendant_monthly":
        if _contains_any(value, ("\u5462", "\u8fd9\u4e2a", "\u90a3\u4e2a", "\u5462?", "\u5462\uff1f", "\u5b58\u91cf", "\u65b0\u589e", "\u4e0b\u964d\u7387")):
            return True
    if "\u88ab\u544a" not in value:
        return False
    return _contains_any(value, ("\u5b58\u91cf", "\u65b0\u589e", "\u4e0b\u964d\u7387", "\u540c\u6bd4", "\u73af\u6bd4", "\u6708\u5ea6\u6307\u6807"))


def _defendant_monthly_metric_kind(text: str, *, recent_spec: dict[str, Any] | None = None) -> str:
    value = str(text or "")
    has_inventory = "\u5b58\u91cf" in value
    has_new = "\u65b0\u589e" in value
    if has_inventory and has_new:
        return "inventory_and_new"
    if has_new:
        return "new"
    if has_inventory:
        return "inventory"
    if recent_spec is not None and recent_spec.get("metric_mode") == "defendant_monthly":
        return str(recent_spec.get("metric_kind") or "inventory")
    return "inventory"


def _looks_like_case_context_followup(text: str, *, recent_spec: dict[str, Any] | None = None) -> bool:
    if recent_spec is None:
        return False
    value = str(text or "").strip()
    if not value:
        return False
    compact = re.sub(r"[\s?？。！!，,；;：:]+", "", value)
    if len(compact) > 12:
        return False
    if "\u5462" not in compact and compact not in {"\u8fd9\u4e2a", "\u90a3\u4e2a"}:
        return False
    return bool(_department_from_query(value) or _assignee_name_from_count_query(value))


def _recent_case_count_spec(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    messages = metadata.get("recent_case_messages") or metadata.get("recent_messages") or ()
    if not isinstance(messages, (list, tuple)):
        return None
    for item in messages:
        text = ""
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("raw_text") or item.get("user_text") or "")
        else:
            text = str(item or "")
        if not text:
            continue
        spec = _case_count_query_spec(text, metadata=None)
        if spec is not None and (spec.get("assignee_name") or spec.get("department") or spec.get("table_type")):
            return spec
    return None


def _defendant_metric_period(spec: dict[str, Any], cutoff: date) -> dict[str, Any]:
    period_type = str(spec.get("period_type") or "month")
    period_start = _parse_date(spec.get("period_start"))
    period_end = _parse_date(spec.get("period_end"))
    if period_type == "quarter" and period_start is not None and period_end is not None:
        effective_end = min(period_end, cutoff) if period_start <= cutoff else period_end
        return {
            "type": "quarter",
            "start": period_start,
            "end": effective_end,
            "label": str(spec.get("period_label") or _quarter_label(period_start)),
        }
    month_start = cutoff.replace(day=1)
    return {"type": "month", "start": month_start, "end": cutoff, "label": cutoff.strftime("%Y-%m")}


def _quarter_label(period_start: date) -> str:
    quarter = (period_start.month - 1) // 3 + 1
    return f"{period_start.year}-Q{quarter}"


def _defendant_monthly_stats(
    rows: list[sqlite3.Row],
    cutoff: date,
    *,
    period_start: date | None = None,
    period_end: date | None = None,
    period_type: str = "month",
) -> dict[str, Any]:
    period_start = period_start or cutoff.replace(day=1)
    period_end = period_end or cutoff
    period_end = min(period_end, cutoff) if period_start <= cutoff else period_end
    previous_cutoff = period_start - timedelta(days=1)
    previous_start = _previous_period_start(period_start, previous_cutoff, period_type=period_type)
    last_year_cutoff = _same_month_day(period_end, years_back=1)
    last_year_start = _same_month_day(period_start, years_back=1)
    inventory_rows = _inventory_rows_as_of(rows, period_end)
    previous_inventory_rows = _inventory_rows_as_of(rows, previous_cutoff)
    last_year_inventory_rows = _inventory_rows_as_of(rows, last_year_cutoff)
    new_rows = _new_rows_in_period(rows, period_start, period_end)
    previous_new_rows = _new_rows_in_period(rows, previous_start, previous_cutoff)
    last_year_new_rows = _new_rows_in_period(rows, last_year_start, last_year_cutoff)
    closed_rows = _closed_rows_in_period(rows, period_start, period_end)
    return {
        "inventory_count": len(inventory_rows),
        "previous_inventory_count": len(previous_inventory_rows),
        "last_year_inventory_count": len(last_year_inventory_rows),
        "inventory_mom_change": _change_rate(len(inventory_rows), len(previous_inventory_rows)),
        "inventory_yoy_change": _change_rate(len(inventory_rows), len(last_year_inventory_rows)),
        "new_count": len(new_rows),
        "previous_new_count": len(previous_new_rows),
        "last_year_new_count": len(last_year_new_rows),
        "new_mom_change": _change_rate(len(new_rows), len(previous_new_rows)),
        "new_yoy_change": _change_rate(len(new_rows), len(last_year_new_rows)),
        "closed_count": len(closed_rows),
        "previous_cutoff_date": previous_cutoff.isoformat(),
        "last_year_cutoff_date": last_year_cutoff.isoformat(),
        "period_type": period_type,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }


def _previous_period_start(period_start: date, previous_cutoff: date, *, period_type: str) -> date:
    if period_type == "quarter":
        month = period_start.month - 3
        year = period_start.year
        if month <= 0:
            month += 12
            year -= 1
        return date(year, month, 1)
    return previous_cutoff.replace(day=1)


def _defendant_metric_sample_rows(
    rows: list[sqlite3.Row],
    cutoff: date,
    *,
    metric_kind: str,
    period_start: date | None = None,
    period_end: date | None = None,
) -> list[sqlite3.Row]:
    period_start = period_start or cutoff.replace(day=1)
    period_end = period_end or cutoff
    period_end = min(period_end, cutoff) if period_start <= cutoff else period_end
    if metric_kind == "new":
        selected = _new_rows_in_period(rows, period_start, period_end)
    else:
        selected = _inventory_rows_as_of(rows, period_end)
    return selected[:20]


def _row_matches_metric_scope(row: sqlite3.Row, spec: dict[str, Any]) -> bool:
    department = str(spec.get("department") or "").strip()
    assignee_name = str(spec.get("assignee_name") or "").strip()
    if department and str(row["department"] or "").strip() != department:
        return False
    if assignee_name and assignee_name not in str(row["assignee_name"] or ""):
        return False
    return True


def _defendant_stats_cutoff(rows: list[sqlite3.Row]) -> date | None:
    dates: list[date] = []
    for row in rows:
        facts = _row_facts(row)
        registered_at = _parse_date(facts.get("\u7cfb\u7edf\u767b\u8bb0\u65e5\u671f"))
        if registered_at is not None:
            dates.append(registered_at)
    return max(dates) if dates else None


def _metadata_current_date(metadata: dict[str, Any] | None) -> date | None:
    if not isinstance(metadata, dict):
        return None
    return _parse_date(metadata.get("current_date"))


def _inventory_rows_as_of(rows: list[sqlite3.Row], cutoff: date) -> list[sqlite3.Row]:
    selected: list[sqlite3.Row] = []
    for row in rows:
        facts = _row_facts(row)
        registered_at = _parse_date(facts.get("\u7cfb\u7edf\u767b\u8bb0\u65e5\u671f"))
        if registered_at is None or registered_at > cutoff:
            continue
        closed = str(facts.get("\u662f\u5426\u7ed3\u6848") or "").strip()
        closed_at = _parse_date(facts.get("\u7ed3\u6848\u65e5\u671f"))
        if closed != "\u662f" or (closed_at is not None and closed_at > cutoff):
            selected.append(row)
    return selected


def _new_rows_in_period(rows: list[sqlite3.Row], start: date, end: date) -> list[sqlite3.Row]:
    selected: list[sqlite3.Row] = []
    for row in rows:
        registered_at = _parse_date(_row_facts(row).get("\u7cfb\u7edf\u767b\u8bb0\u65e5\u671f"))
        if registered_at is not None and start <= registered_at <= end:
            selected.append(row)
    return selected


def _closed_rows_in_period(rows: list[sqlite3.Row], start: date, end: date) -> list[sqlite3.Row]:
    selected: list[sqlite3.Row] = []
    for row in rows:
        facts = _row_facts(row)
        if str(facts.get("\u662f\u5426\u7ed3\u6848") or "").strip() != "\u662f":
            continue
        closed_at = _parse_date(facts.get("\u7ed3\u6848\u65e5\u671f"))
        if closed_at is not None and start <= closed_at <= end:
            selected.append(row)
    return selected


def _row_facts(row: sqlite3.Row) -> dict[str, Any]:
    try:
        return json.loads(str(row["facts_json"] or "{}"))
    except (KeyError, json.JSONDecodeError):
        return {}


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    raw = raw[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _same_month_day(value: date, *, years_back: int) -> date:
    target_year = value.year - years_back
    day = value.day
    while day > 0:
        try:
            return date(target_year, value.month, day)
        except ValueError:
            day -= 1
    return date(target_year, value.month, 1)


def _change_rate(current: int, baseline: int) -> float | None:
    if baseline == 0:
        return None
    return (current - baseline) / baseline * 100


def _defendant_metric_label(metric_kind: str) -> str:
    if metric_kind == "new":
        return "\u65b0\u589e"
    if metric_kind == "inventory_and_new":
        return "\u5b58\u91cf/\u65b0\u589e"
    return "\u5b58\u91cf"


def _row_matches_status(row: sqlite3.Row, status_filter: str) -> bool:
    if status_filter == "unclosed":
        return _row_is_unclosed(row)
    if status_filter == "closed":
        return not _row_is_unclosed(row)
    return True


def _row_is_unclosed(row: sqlite3.Row) -> bool:
    try:
        facts = json.loads(str(row["facts_json"] or "{}"))
    except json.JSONDecodeError:
        facts = {}
    closed = str(facts.get("\u662f\u5426\u7ed3\u6848") or "").strip()
    if closed == "\u5426":
        return True
    if closed == "\u662f":
        return False
    status = str(row["status"] or "").strip()
    return status not in {"\u5df2\u7ed3\u6848", "\u7ed3\u6848", "\u5ba1\u7ed3", "\u5c65\u884c"}


def _status_filter_label(status_filter: str) -> str:
    if status_filter == "unclosed":
        return "\u672a\u7ed3\u6848"
    if status_filter == "closed":
        return "\u5df2\u7ed3\u6848"
    return ""


def _last_name_token(text: str) -> str:
    tokens = re.findall(r"[\u4e00-\u9fff]{2,4}", str(text or ""))
    stop = {
        "\u5e2e\u6211",
        "\u770b\u4e0b",
        "\u67e5\u4e0b",
        "\u67e5\u4e00",
        "\u7edf\u8ba1",
        "发我",
        "给我",
        "发下",
        "发给",
        "总体",
        "整体",
        "总量",
        "总数",
        "总共",
        "\u88ab\u544a",
        "\u539f\u544a",
        "\u6848\u4ef6",
        "\u591a\u5c11",
    }
    candidates = [token for token in tokens if token not in stop and not token.endswith(("\u6848", "\u6848\u4ef6"))]
    return candidates[-1] if candidates else ""


def _is_likely_assignee_name(value: str) -> bool:
    name = str(value or "").strip()
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", name):
        return False
    if _department_from_query(name):
        return False
    return not _contains_any(
        name,
        (
            "\u76ee\u524d",
            "\u5f53\u524d",
            "\u73b0\u5728",
            "\u5168\u90e8",
            "\u6240\u6709",
            "总体",
            "整体",
            "总量",
            "总数",
            "总共",
            "全中心",
            "法务中心",
            "全条线",
            "季度",
            "月度",
            "数据",
            "同比",
            "环比",
            "发我",
            "给我",
            "发给",
            "看下",
            "查下",
            "\u6848\u4ef6",
            "\u672a\u7ed3",
            "\u5df2\u7ed3",
            "\u7ed3\u6848",
            "\u672a\u529e",
            "\u5728\u529e",
            "\u591a\u5c11",
            "\u51e0\u4ef6",
            "\u51e0\u4e2a",
            "\u54ea\u4e9b",
            "\u4ec0\u4e48",
            "\u56e2\u961f",
            "\u90e8\u95e8",
            "\u6cd5\u52a1",
        ),
    )


def _table_type_label(table_type: str) -> str:
    if table_type == "defendant_case_table":
        return "\u88ab\u544a"
    if table_type == "plaintiff_case_table":
        return "\u539f\u544a"
    return ""


def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
    return any(marker in str(value or "") for marker in markers)


def _fts_terms(query: str) -> list[str]:
    terms = _like_terms(query)
    result: list[str] = []
    for term in terms:
        if len(term) < 2:
            continue
        escaped = term.replace('"', '""')
        result.append(f'"{escaped}"')
    return result[:10]


def _like_terms(query: str) -> list[str]:
    compact = re.sub(r"\s+", "", str(query or ""))
    parts = [
        part
        for part in re.split(r"[，,。；;：:？?！!\s]+", str(query or ""))
        if len(part.strip()) >= 2
    ]
    if compact and compact not in parts:
        parts.insert(0, compact)
    stop_words = ("我", "想", "查", "一下", "看看", "几个", "几件", "有没有", "是什么")
    cleaned: list[str] = []
    for part in parts:
        value = part.strip()
        for stop in stop_words:
            value = value.replace(stop, "")
        if len(value) >= 2 and value not in cleaned:
            cleaned.append(value)
        for gram in _han_ngrams(value):
            if gram not in cleaned:
                cleaned.append(gram)
    return cleaned[:12]


def _han_ngrams(value: str) -> list[str]:
    text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", value)
    if len(text) < 3:
        return []
    grams: list[str] = []
    for size in (4, 3, 2):
        for index in range(0, max(0, len(text) - size + 1)):
            gram = text[index : index + size]
            if len(gram) == size and gram not in grams:
                grams.append(gram)
        if len(grams) >= 8:
            break
    return grams
