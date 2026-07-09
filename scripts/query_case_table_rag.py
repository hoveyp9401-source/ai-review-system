from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.agent2.case_table_rag import DEFAULT_CASE_RAG_INDEX, search_case_table_index


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the local Agent2 case-table RAG index.")
    parser.add_argument("query", help="Case keyword, assignee, case number, company, or matter description.")
    parser.add_argument("--index", default=str(DEFAULT_CASE_RAG_INDEX), help="Path to case_index.sqlite.")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    documents = search_case_table_index(Path(args.index), args.query, limit=args.limit)
    print(
        json.dumps(
            [
                {
                    "doc_id": item.doc_id,
                    "case_name": item.case_name,
                    "department": item.department,
                    "assignee_name": item.assignee_name,
                    "source_file": item.source_file,
                    "sheet_name": item.sheet_name,
                    "row_number": item.row_number,
                    "table_type": item.table_type,
                }
                for item in documents
            ],
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
