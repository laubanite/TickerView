"""SQLite 数据层:建表、upsert、抓取日志。

设计要点:日线每次全区间拉取后整窗覆盖(INSERT OR REPLACE),规避前复权价随
最新价漂移导致的历史行过期问题。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

from .config import PROJECT_ROOT

DEFAULT_DB = PROJECT_ROOT / "data" / "alphaprism.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS etf (
    symbol     TEXT PRIMARY KEY,
    name       TEXT,
    category   TEXT,
    in_pool    INTEGER NOT NULL DEFAULT 1,
    added_at   TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS etf_kline_daily (
    symbol     TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    volume     REAL,
    amount     REAL,
    pct_chg    REAL,
    turnover   REAL,
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS etf_kline_weekly (
    symbol     TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    volume     REAL,
    amount     REAL,
    pct_chg    REAL,
    turnover   REAL,
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS etf_kline_30m (
    symbol TEXT NOT NULL,
    ts     TEXT NOT NULL,
    open   REAL,
    high   REAL,
    low    REAL,
    close  REAL,
    volume REAL,
    amount REAL,
    PRIMARY KEY (symbol, ts)
);

CREATE TABLE IF NOT EXISTS etf_fund_scale (
    symbol     TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    shares     REAL,
    scale      REAL,
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS industry_data (
    trade_date TEXT NOT NULL,
    industry   TEXT NOT NULL,
    field      TEXT NOT NULL,
    value      REAL,
    PRIMARY KEY (trade_date, industry, field)
);

CREATE TABLE IF NOT EXISTS sector_turnover (
    symbol        TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    sector        TEXT NOT NULL,
    sector_amount REAL,
    market_amount REAL,
    ratio_pct     REAL,
    level         TEXT,
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS sector_valuation (
    trade_date TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    sector     TEXT NOT NULL,
    n_stocks   INTEGER,
    n_pe       INTEGER,
    n_pb       INTEGER,
    med_pe_ttm REAL,
    med_pb_mrq REAL,
    mkt_med_pe REAL,
    mkt_med_pb REAL,
    rel_pb     REAL,
    reading    TEXT,
    PRIMARY KEY (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS catalyst_anomaly (
    trade_date TEXT NOT NULL,
    thscode    TEXT NOT NULL,
    stock_name TEXT,
    tag_name   TEXT,
    keywords   TEXT,
    analysis   TEXT,
    symbol     TEXT,
    PRIMARY KEY (trade_date, thscode)
);

CREATE TABLE IF NOT EXISTS catalyst_hot (
    trade_date  TEXT NOT NULL,
    thscode     TEXT NOT NULL,
    name        TEXT,
    rank        INTEGER,
    heat        TEXT,
    rank_change INTEGER,
    symbol      TEXT,
    PRIMARY KEY (trade_date, thscode)
);

CREATE TABLE IF NOT EXISTS signal_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    kind       TEXT NOT NULL,   -- appear=信号出现 / activate=确认升级 / fail=失效
    signal     TEXT NOT NULL,   -- 买点 / 卖点
    state      TEXT NOT NULL,   -- 观察 / 激活 / 失效
    price      REAL,
    support    REAL,
    resistance REAL,
    note       TEXT,
    result     TEXT             -- 用户填写: 止盈/止损/持有/放弃/回避/踏空
);

CREATE TABLE IF NOT EXISTS catalyst_status (
    trade_date TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    status     TEXT NOT NULL,   -- 增强/减弱/新增/未变
    reason     TEXT,
    PRIMARY KEY (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS monitor_trigger (
    symbol    TEXT NOT NULL,
    trigger   TEXT NOT NULL,    -- buy_confirm/pullback_confirm/buy_invalid/sell_confirm/support_break
    trade_date TEXT NOT NULL,   -- 最近触发交易日
    active    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, trigger)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts TEXT NOT NULL,
    source TEXT NOT NULL,
    scope  TEXT NOT NULL,
    start  TEXT,
    end    TEXT,
    rows   INTEGER,
    status TEXT NOT NULL,
    error  TEXT
);

CREATE TABLE IF NOT EXISTS important_news (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date  TEXT NOT NULL,
    sector      TEXT NOT NULL,
    symbol      TEXT,
    text        TEXT NOT NULL,
    impact      TEXT NOT NULL,
    confidence  TEXT,
    type        TEXT,
    source_grade TEXT,
    cross       INTEGER DEFAULT 1,
    source      TEXT,
    keywords    TEXT,           -- 命中的关键词(JSON 数组),供 M5 关键词调优
    fetched_at  TEXT,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (trade_date, text)
);

CREATE TABLE IF NOT EXISTS catalyst_verification (
    news_id     INTEGER NOT NULL,     -- 关联 important_news.id
    n_day       INTEGER NOT NULL,     -- 3 / 20
    outcome     TEXT NOT NULL,        -- 应验|部分应验|未应验|无法判定
    evidence    TEXT,
    excess      REAL,                 -- 板块N日超额(相对上证),供 stats/alpha
    verified_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (news_id, n_day)
);

-- 盘中深入分析 v2.0(M1' 归档 + 盘后增量4 建议验证):建议存档 / 建议验证 / 持仓卡
CREATE TABLE IF NOT EXISTS advice_archive (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date   TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    created_at   TEXT NOT NULL,       -- 生成时点 HH:MM
    anchor_price REAL,                -- 决策锚定价(当日固定)
    scenario     TEXT,                -- 五级场景:破位/突破异动日/变盘前兆/区间震荡/趋势
    state_word   TEXT,                -- 状态词(布尔词汇表白名单)
    risk_level   TEXT,                -- 正常/关注/升级(风险上升语义,三段式 §4.3)
    category     TEXT,                -- 建议类别:加仓/买入/试多|减仓/砍仓/清仓|持有/观望/等待确认
    advice_md    TEXT,                -- 深入分析全文
    snapshot_md  TEXT,                -- 数据快照全文(白名单源)
    degraded     INTEGER DEFAULT 0,   -- 1=降级规则信号
    verdict      TEXT                 -- 盘后增量4 回填:应验/部分应验/未应验/无法判定
);

CREATE TABLE IF NOT EXISTS advice_verification (
    archive_id  INTEGER NOT NULL,     -- 关联 advice_archive.id
    n_day       INTEGER NOT NULL,     -- 1 / 3 / 5 / 10(默认 5 为主、3 参考,1/10 为短线/中期参考)
    outcome     TEXT NOT NULL,        -- 应验|部分应验|未应验|无法判定
    move_pct    REAL,                 -- N日后收盘相对锚定价涨跌%
    evidence    TEXT,
    direction_correct INTEGER          -- 方向对错(借鉴 decision_signal:应验/部分应验=1,未应验=0,无法判定=NULL)
    verified_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (archive_id, n_day)
);

CREATE TABLE IF NOT EXISTS holdings (
    symbol     TEXT PRIMARY KEY,      -- ETF 代码
    name       TEXT,
    cost       REAL NOT NULL,         -- 单位成本(前复权口径)
    quantity   INTEGER NOT NULL,      -- 股数
    status     TEXT DEFAULT '持仓',   -- 持仓/锚点(100股)/观察
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS account_meta (
    key   TEXT PRIMARY KEY,           -- total_capital / cash / ...
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
"""

# 固定列顺序,供 upsert 使用
KLINE_COLUMNS = [
    "symbol", "trade_date", "open", "high", "low", "close",
    "volume", "amount", "pct_chg", "turnover",
]
KLINE30M_COLUMNS = ["symbol", "ts", "open", "high", "low", "close", "volume", "amount"]
SCALE_COLUMNS = ["symbol", "trade_date", "shares", "scale"]
SECTOR_COLUMNS = ["symbol", "trade_date", "sector", "sector_amount", "market_amount",
                  "ratio_pct", "level"]
VALUATION_COLUMNS = ["trade_date", "symbol", "sector", "n_stocks", "n_pe", "n_pb",
                     "med_pe_ttm", "med_pb_mrq", "mkt_med_pe", "mkt_med_pb", "rel_pb", "reading"]
CATALYST_ANOMALY_COLUMNS = ["trade_date", "thscode", "stock_name", "tag_name",
                            "keywords", "analysis", "symbol"]
CATALYST_HOT_COLUMNS = ["trade_date", "thscode", "name", "rank", "heat", "rank_change", "symbol"]
SIGNAL_LOG_COLUMNS = ["trade_date", "symbol", "kind", "signal", "state", "price",
                      "support", "resistance", "note", "result"]
CATALYST_STATUS_COLUMNS = ["trade_date", "symbol", "status", "reason",
                           "impact", "confidence", "type", "source_grade", "cross", "fetched_at"]

_CATALYST_STATUS_EXTRA = {
    "impact": "TEXT",
    "confidence": "TEXT",
    "type": "TEXT",
    "source_grade": "TEXT",
    "cross": "INTEGER",
    "fetched_at": "TEXT",
}


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _migrate_catalyst_status(conn)
    conn.commit()


def _migrate_catalyst_status(conn: sqlite3.Connection) -> None:
    """catalyst_status 扩列(方案 M4):已有库 ALTER 补列(impact/confidence/type 等)。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(catalyst_status)").fetchall()}
    for name, decl in _CATALYST_STATUS_EXTRA.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE catalyst_status ADD COLUMN {name} {decl}")


def upsert_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence],
) -> int:
    """整窗覆盖式 upsert(INSERT OR REPLACE),返回写入行数。"""
    rows = list(rows)
    if not rows:
        return 0
    placeholders = ",".join("?" * len(columns))
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
    conn.executemany(sql, rows)
    return len(rows)


def upsert_etf(conn: sqlite3.Connection, symbol: str, name: str, category: str) -> None:
    conn.execute(
        """
        INSERT INTO etf (symbol, name, category, in_pool, added_at, updated_at)
        VALUES (?, ?, ?, 1, datetime('now','localtime'), datetime('now','localtime'))
        ON CONFLICT(symbol) DO UPDATE SET
            name = excluded.name,
            category = excluded.category,
            updated_at = datetime('now','localtime')
        """,
        (symbol, name, category),
    )


def sync_watchlist(conn: sqlite3.Connection, symbols: list[str]) -> None:
    """etf.in_pool 与配置跟踪池对齐:池内=1,池外=0。"""
    conn.execute("UPDATE etf SET in_pool = 0")
    if symbols:
        placeholders = ",".join("?" * len(symbols))
        conn.execute(f"UPDATE etf SET in_pool = 1 WHERE symbol IN ({placeholders})", symbols)


def log_fetch(
    conn: sqlite3.Connection,
    source: str,
    scope: str,
    start: str | None,
    end: str | None,
    rows: int | None,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO fetch_log (run_ts, source, scope, start, end, rows, status, error)
        VALUES (datetime('now','localtime'), ?, ?, ?, ?, ?, ?, ?)
        """,
        (source, scope, start, end, rows, status, error),
    )
