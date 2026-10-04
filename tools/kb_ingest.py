"""Command-line entry point for local documentation ingestion."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from netops_ai.docs_kb.ingest import ingest


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest local vendor docs into SQLite FTS5.")
    parser.add_argument("corpus_dir", help="Directory containing local .md/.txt/.html/.htm/.pdf files")
    parser.add_argument("--db", default="records/docs_kb.db", help="SQLite database path")
    parser.add_argument("--vendor", default="cisco", help="Vendor label")
    args = parser.parse_args()
    summary = ingest(args.corpus_dir, args.db, vendor=args.vendor)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if summary["pdf_skipped"]:
        print("提示：部分 PDF 因未安装 pypdf 被跳过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
