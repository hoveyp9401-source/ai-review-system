from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import pandas as pd

from app.agent2.case_table_rag import CaseTableDocument, write_case_table_index


DEFAULT_RAW_ROOT = Path("data/rag_sources/raw")
DEFAULT_OUTPUT_DIR = Path("data/rag_indexes/case_tables")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Agent2 case-table RAG index from archived Excel sources.")
    parser.add_argument("--raw-root", default=str(DEFAULT_RAW_ROOT), help="Directory containing dated raw source folders.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for SQLite/JSONL index outputs.")
    parser.add_argument("--refresh-date", default="", help="Optional refresh date label, e.g. 2026-07-06.")
    args = parser.parse_args()

    raw_root = Path(args.raw_root)
    source_files = _case_source_files(raw_root)
    if not source_files:
        raise SystemExit(f"No case source workbooks found under {raw_root}")

    documents: list[CaseTableDocument] = []
    source_summaries: list[dict[str, Any]] = []
    for source_path in source_files:
        extracted = extract_documents_from_workbook(source_path, refresh_date=args.refresh_date)
        documents.extend(extracted)
        source_summaries.append(
            {
                "file": str(source_path),
                "sha256": _sha256(source_path),
                "size_bytes": source_path.stat().st_size,
                "document_count": len(extracted),
            }
        )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = write_case_table_index(
        documents,
        sqlite_path=output_dir / "case_index.sqlite",
        jsonl_path=output_dir / "case_documents.jsonl",
        metadata={
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "refresh_date": args.refresh_date,
            "sources": source_summaries,
        },
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


def extract_documents_from_workbook(source_path: Path, *, refresh_date: str = "") -> list[CaseTableDocument]:
    table_type = _table_type(source_path)
    documents: list[CaseTableDocument] = []
    workbook = pd.ExcelFile(source_path)
    for sheet_name in workbook.sheet_names:
        frame = pd.read_excel(source_path, sheet_name=sheet_name, header=0, dtype=object)
        if frame.empty:
            continue
        frame = frame.dropna(how="all")
        if frame.empty:
            continue
        headers = [_clean_header(column, index) for index, column in enumerate(frame.columns)]
        frame.columns = headers
        for zero_index, row in frame.iterrows():
            facts = _row_facts(headers, row)
            if not facts:
                continue
            case_name = _first_fact(facts, ("案件名称", "案名")) or _first_fact(facts, ("受理案由", "案由"))
            text = _row_text(facts)
            if not case_name and "案" not in text:
                continue
            row_number = int(zero_index) + 2
            doc_id = _doc_id(source_path, sheet_name, row_number, text)
            documents.append(
                CaseTableDocument(
                    doc_id=doc_id,
                    source_type="case_table_rag",
                    source_file=source_path.name,
                    sheet_name=str(sheet_name),
                    row_number=row_number,
                    table_type=table_type,
                    case_name=case_name,
                    department=_first_fact(facts, ("法务部门", "部门")),
                    assignee_name=_assignee_name(facts),
                    status=_first_fact(facts, ("案件状态", "当前阶段", "阶段", "红黄绿", "状态")),
                    updated_at=refresh_date,
                    text=text,
                    facts=facts,
                )
            )
    return documents


def _case_source_files(raw_root: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in sorted(raw_root.glob("**/*.xlsx")):
        name = path.name
        if "案件" not in name:
            continue
        if "原告" in name or "被告" in name:
            candidates.append(path)
    latest_by_name: dict[str, Path] = {}
    for path in candidates:
        latest_by_name[path.name] = path
    return sorted(latest_by_name.values())


def _table_type(path: Path) -> str:
    name = path.name
    if "原告" in name:
        return "plaintiff_case_table"
    if "被告" in name:
        return "defendant_case_table"
    return "case_table"


def _clean_header(value: Any, index: int) -> str:
    text = _clean_cell(value)
    if not text or text.lower().startswith("unnamed:"):
        return f"列{index + 1}"
    return text


def _row_facts(headers: list[str], row: Any) -> dict[str, str]:
    facts: dict[str, str] = {}
    for header in headers:
        value = _clean_cell(row.get(header))
        if not value:
            continue
        facts[header] = value
    return facts


def _row_text(facts: dict[str, str]) -> str:
    preferred: list[str] = []
    remaining: list[str] = []
    for key, value in facts.items():
        item = f"{key}: {value}"
        if _is_preferred_field(key):
            preferred.append(item)
        else:
            remaining.append(item)
    return "；".join([*preferred, *remaining])


def _is_preferred_field(header: str) -> bool:
    return any(
        marker in header
        for marker in (
            "案件",
            "案号",
            "法务",
            "负责人",
            "承办",
            "公司",
            "分公司",
            "法院",
            "仲裁",
            "执行",
            "开庭",
            "阶段",
            "计划",
            "完成情况",
            "状态",
            "金额",
        )
    )


def _first_fact(facts: dict[str, str], markers: tuple[str, ...]) -> str:
    for key, value in facts.items():
        if any(marker in key for marker in markers):
            return value
    return ""


def _assignee_name(facts: dict[str, str]) -> str:
    for key, value in facts.items():
        if any(marker in key for marker in ("负责人", "承办人", "承办", "经办")):
            return value
    return ""


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    text = re.sub(r"\s+", " ", text)
    if text.endswith(".0") and re.fullmatch(r"\d+\.0", text):
        return text[:-2]
    return text


def _doc_id(source_path: Path, sheet_name: str, row_number: int, text: str) -> str:
    payload = f"{source_path.name}|{sheet_name}|{row_number}|{text[:200]}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


if __name__ == "__main__":
    main()
