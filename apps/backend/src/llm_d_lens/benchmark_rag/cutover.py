"""Back up and remove legacy SQL RAG tables, leaving empty Prism-only storage.

This command never accesses OpenSearch, rebuilds indexes or imports Prism data.
The operator must quiesce application writers before running it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from llm_d_lens.db.dao.rag_transfer import reset_legacy_rag_storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--writers-quiesced", action="store_true", required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = reset_legacy_rag_storage(args.backup_dir, writers_quiesced=args.writers_quiesced)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
