"""初始化数据库:建表。运行:python scripts/init_db.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphaprism.db import DEFAULT_DB, connect, init_db


def main() -> None:
    conn = connect()
    init_db(conn)
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    print(f"数据库: {DEFAULT_DB}")
    print(f"已建表({len(tables)}): {', '.join(tables)}")
    conn.close()


if __name__ == "__main__":
    main()
