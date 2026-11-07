from __future__ import annotations
import json, sqlite3
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
from .models import CompanyContext, ScreenedCompany

SCHEMA = '''
CREATE TABLE IF NOT EXISTS companies (
 market TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT, external_id TEXT,
 aliases_json TEXT, source TEXT, updated_at TEXT, PRIMARY KEY (market,symbol));
CREATE TABLE IF NOT EXISTS quarterly_fundamentals (
 market TEXT NOT NULL, symbol TEXT NOT NULL, period TEXT NOT NULL,
 revenue REAL, net_income REAL, cfo REAL, capex REAL, fcf REAL,
 buybacks REAL, diluted_shares REAL, net_margin REAL, source TEXT,
 updated_at TEXT, PRIMARY KEY (market,symbol,period));
CREATE TABLE IF NOT EXISTS scan_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
 markets TEXT, criteria_version TEXT);
CREATE TABLE IF NOT EXISTS screen_results (
 run_id INTEGER NOT NULL, market TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT,
 matches INTEGER, surfaced INTEGER, price REAL, intrinsic_value REAL,
 upside_to_intrinsic REAL, criteria_json TEXT, metadata_json TEXT,
 PRIMARY KEY (run_id,market,symbol));

CREATE TABLE IF NOT EXISTS technical_scan_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
 market TEXT NOT NULL DEFAULT 'USA', source TEXT, ticker_count INTEGER,
 rsi_limit REAL, macd_pct_limit REAL);

CREATE TABLE IF NOT EXISTS technical_results (
 scan_id INTEGER NOT NULL, market TEXT NOT NULL DEFAULT 'USA',
 symbol TEXT NOT NULL, price REAL,
 rsi_1d REAL, macd_pct_1d REAL, rsi_4h REAL, macd_pct_4h REAL,
 rsi_1h REAL, macd_pct_1h REAL, score INTEGER, evaluable INTEGER,
 matched INTEGER, alert_triggered INTEGER, conditions_json TEXT,
 bar_1d TEXT, bar_4h TEXT, bar_1h TEXT,
 bars_1d INTEGER, bars_4h INTEGER, bars_1h INTEGER, source TEXT,
 quality_json TEXT,
 PRIMARY KEY (scan_id,symbol));

CREATE TABLE IF NOT EXISTS technical_alert_state (
 symbol TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 0,
 first_triggered_at TEXT, last_triggered_at TEXT, last_cleared_at TEXT,
 updated_at TEXT);

CREATE TABLE IF NOT EXISTS technical_alert_state_market (
 market TEXT NOT NULL, symbol TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 0,
 first_triggered_at TEXT, last_triggered_at TEXT, last_cleared_at TEXT,
 updated_at TEXT, PRIMARY KEY (market,symbol));
'''

def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    _migrate_technical_schema(conn)
    conn.commit()
    return conn


def _table_columns(conn, table):
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_technical_schema(conn):
    """Migra bases V2.11 a V2.12 sin borrar datos existentes."""
    run_cols = _table_columns(conn, "technical_scan_runs")
    if "market" not in run_cols:
        conn.execute("ALTER TABLE technical_scan_runs ADD COLUMN market TEXT NOT NULL DEFAULT 'USA'")

    result_cols = _table_columns(conn, "technical_results")
    if "market" not in result_cols:
        conn.execute("ALTER TABLE technical_results ADD COLUMN market TEXT NOT NULL DEFAULT 'USA'")
    if "quality_json" not in result_cols:
        conn.execute("ALTER TABLE technical_results ADD COLUMN quality_json TEXT")

    conn.execute(
        """CREATE TABLE IF NOT EXISTS technical_alert_state_market (
             market TEXT NOT NULL, symbol TEXT NOT NULL,
             active INTEGER NOT NULL DEFAULT 0,
             first_triggered_at TEXT, last_triggered_at TEXT,
             last_cleared_at TEXT, updated_at TEXT,
             PRIMARY KEY (market,symbol))"""
    )
    # Preservar el anti-spam de V2.11 como estado USA.
    legacy_cols = _table_columns(conn, "technical_alert_state")
    if "symbol" in legacy_cols:
        conn.execute(
            """INSERT OR IGNORE INTO technical_alert_state_market(
                 market,symbol,active,first_triggered_at,last_triggered_at,
                 last_cleared_at,updated_at)
               SELECT 'USA',symbol,active,first_triggered_at,last_triggered_at,
                      last_cleared_at,updated_at
               FROM technical_alert_state"""
        )

def now_iso(): return datetime.now(timezone.utc).isoformat()

def upsert_company(conn, market, symbol, name, external_id=None, aliases=None, source=""):
    conn.execute('''INSERT INTO companies VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(market,symbol) DO UPDATE SET name=excluded.name,
      external_id=excluded.external_id,aliases_json=excluded.aliases_json,
      source=excluded.source,updated_at=excluded.updated_at''',
      (market,symbol,name,external_id,json.dumps(aliases or []),source,now_iso()))

def store_context(conn, ctx: CompanyContext, source):
    for i,p in enumerate(ctx.quarters):
        def at(a): return None if i>=len(a) or a[i] is None else float(a[i])
        rev,ni,cfo,capex,fcf,buy,shr = map(at,[ctx.revenue,ctx.net_income,ctx.cfo,ctx.capex,ctx.fcf,ctx.buybacks,ctx.diluted_shares])
        margin = ni/rev if ni is not None and rev not in (None,0) else None
        conn.execute('''INSERT INTO quarterly_fundamentals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(market,symbol,period) DO UPDATE SET revenue=excluded.revenue,
          net_income=excluded.net_income,cfo=excluded.cfo,capex=excluded.capex,
          fcf=excluded.fcf,buybacks=excluded.buybacks,diluted_shares=excluded.diluted_shares,
          net_margin=excluded.net_margin,source=excluded.source,updated_at=excluded.updated_at''',
          (ctx.market,ctx.symbol,p,rev,ni,cfo,capex,fcf,buy,shr,margin,source,now_iso()))

def create_run(conn, markets):
    cur=conn.execute("INSERT INTO scan_runs(started_at,markets,criteria_version) VALUES(?,?,?)",
                     (now_iso(),','.join(markets),'criteria.py')); conn.commit(); return int(cur.lastrowid)

def finish_run(conn, run_id):
    conn.execute("UPDATE scan_runs SET finished_at=? WHERE id=?",(now_iso(),run_id)); conn.commit()

def store_result(conn, run_id, r: ScreenedCompany):
    cj=json.dumps([{"key":c.key,"label":c.label,"passed":c.passed,"value":c.value,"reason":c.reason,"evaluable":getattr(c,"evaluable",True)} for c in r.criteria],default=str)
    conn.execute('''INSERT OR REPLACE INTO screen_results VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
      (run_id,r.market,r.symbol,r.name,r.matches,int(r.surfaced),r.price,r.intrinsic_value,
       r.upside_to_intrinsic,cj,json.dumps(r.metadata,default=str)))

def latest_results(conn, market=None):
    """
    Combina la última corrida TERMINADA de USA y ARG.

    Se toma la última corrida desde scan_runs (fuente de verdad de ejecución),
    no desde screen_results. Esto evita que una corrida vacía/rota haga que
    reaparezcan resultados antiguos de un mercado.

    market:
        None  -> ambos mercados
        'USA' -> sólo USA
        'ARG' -> sólo Argentina
    """
    sql = """
    WITH latest_runs AS (
      SELECT
        markets AS market,
        MAX(id) AS run_id
      FROM scan_runs
      WHERE finished_at IS NOT NULL
        AND markets IN ('USA', 'ARG')
      GROUP BY markets
    )
    SELECT sr.*
    FROM latest_runs lr
    JOIN screen_results sr
      ON sr.run_id = lr.run_id
     AND sr.market = lr.market
    """
    params = []

    if market in ('USA', 'ARG'):
        sql += " WHERE sr.market=?"
        params.append(market)

    return pd.read_sql_query(sql, conn, params=params)


def latest_run_stats(conn):
    """
    Resumen de la última corrida terminada por mercado.
    Sirve para que la UI muestre claramente si Argentina fue escaneada y
    cuántos resultados produjo.
    """
    sql = """
    WITH latest_runs AS (
      SELECT markets AS market, MAX(id) AS run_id
      FROM scan_runs
      WHERE finished_at IS NOT NULL
        AND markets IN ('USA', 'ARG')
      GROUP BY markets
    )
    SELECT
      lr.market,
      lr.run_id,
      r.started_at,
      r.finished_at,
      COUNT(sr.symbol) AS result_count,
      COALESCE(SUM(CASE WHEN sr.surfaced=1 THEN 1 ELSE 0 END), 0) AS surfaced_count
    FROM latest_runs lr
    JOIN scan_runs r ON r.id=lr.run_id
    LEFT JOIN screen_results sr
      ON sr.run_id=lr.run_id AND sr.market=lr.market
    GROUP BY lr.market, lr.run_id, r.started_at, r.finished_at
    ORDER BY lr.market
    """
    return pd.read_sql_query(sql, conn)

def company_count(conn):
    return pd.read_sql_query("SELECT market,COUNT(*) companies FROM companies GROUP BY market ORDER BY market",conn)


def latest_known_prices(conn, market, symbols=None):
    """Último precio no nulo guardado por ticker, para fallback si Yahoo falla."""
    sql = """
    SELECT sr.market, sr.symbol, sr.price
    FROM screen_results sr
    JOIN (
      SELECT market, symbol, MAX(run_id) AS run_id
      FROM screen_results
      WHERE price IS NOT NULL AND price > 0
      GROUP BY market, symbol
    ) x ON x.market=sr.market AND x.symbol=sr.symbol AND x.run_id=sr.run_id
    WHERE sr.market=? AND sr.price IS NOT NULL AND sr.price > 0
    """
    df = pd.read_sql_query(sql, conn, params=[market])
    result = {str(r.symbol): float(r.price) for _, r in df.iterrows()}
    if symbols is not None:
        wanted = set(symbols)
        result = {k:v for k,v in result.items() if k in wanted}
    return result


# ==========================================================================
# TECHNICAL ALERTS — MULTI-MARKET
# ==========================================================================

def latest_market_watchlist(conn, market="USA", limit=100, priority_only=False):
    """Tickers de la última corrida terminada del mercado solicitado."""
    market = str(market or "USA").upper().strip()
    if market not in {"USA", "ARG"}:
        raise ValueError("market debe ser USA o ARG")

    sql = """
    WITH latest_run AS (
      SELECT MAX(id) AS run_id
      FROM scan_runs
      WHERE finished_at IS NOT NULL AND markets=?
    )
    SELECT sr.symbol, sr.name, sr.matches, sr.surfaced,
           sr.price, sr.intrinsic_value, sr.upside_to_intrinsic
    FROM screen_results sr
    JOIN latest_run lr ON lr.run_id=sr.run_id
    WHERE sr.market=?
    """
    params = [market, market]

    if priority_only:
        sql += " AND sr.surfaced=1 "

    sql += """
    ORDER BY sr.surfaced DESC,
             sr.matches DESC,
             CASE WHEN sr.upside_to_intrinsic IS NULL THEN 1 ELSE 0 END,
             sr.upside_to_intrinsic DESC,
             sr.symbol ASC
    LIMIT ?
    """
    params.append(int(limit))
    return pd.read_sql_query(sql, conn, params=params)


def latest_usa_watchlist(conn, limit=100, priority_only=False):
    return latest_market_watchlist(conn, "USA", limit, priority_only)


def latest_arg_watchlist(conn, limit=100, priority_only=False):
    return latest_market_watchlist(conn, "ARG", limit, priority_only)


def create_technical_run(
    conn, source, ticker_count, rsi_limit, macd_pct_limit, market="USA"
):
    market = str(market or "USA").upper().strip()
    cur = conn.execute(
        """INSERT INTO technical_scan_runs(
             started_at,market,source,ticker_count,rsi_limit,macd_pct_limit
           ) VALUES(?,?,?,?,?,?)""",
        (
            now_iso(), market, source, int(ticker_count),
            float(rsi_limit), float(macd_pct_limit),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_technical_run(conn, scan_id):
    conn.execute(
        "UPDATE technical_scan_runs SET finished_at=? WHERE id=?",
        (now_iso(), int(scan_id)),
    )
    conn.commit()


def update_technical_alert_state(conn, market, symbol, matched):
    """Anti-spam aislado por market+symbol. True sólo al ENTRAR en 5/5."""
    market = str(market or "USA").upper().strip()
    symbol = str(symbol).upper().strip()

    row = conn.execute(
        """SELECT active,first_triggered_at,last_triggered_at
           FROM technical_alert_state_market
           WHERE market=? AND symbol=?""",
        (market, symbol),
    ).fetchone()

    active = bool(row[0]) if row else False
    first = row[1] if row else None
    previous_last = row[2] if row else None
    now = now_iso()
    triggered = False

    if matched:
        if not active:
            triggered = True
            first = first or now
            last_triggered = now
        else:
            last_triggered = previous_last

        conn.execute(
            """INSERT INTO technical_alert_state_market(
                 market,symbol,active,first_triggered_at,last_triggered_at,
                 last_cleared_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(market,symbol) DO UPDATE SET
                 active=1,
                 first_triggered_at=COALESCE(technical_alert_state_market.first_triggered_at,excluded.first_triggered_at),
                 last_triggered_at=COALESCE(excluded.last_triggered_at,technical_alert_state_market.last_triggered_at),
                 updated_at=excluded.updated_at""",
            (market, symbol, 1, first, last_triggered, None, now),
        )
    else:
        conn.execute(
            """INSERT INTO technical_alert_state_market(
                 market,symbol,active,first_triggered_at,last_triggered_at,
                 last_cleared_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(market,symbol) DO UPDATE SET
                 active=0,
                 last_cleared_at=CASE
                   WHEN technical_alert_state_market.active=1
                   THEN excluded.last_cleared_at
                   ELSE technical_alert_state_market.last_cleared_at END,
                 updated_at=excluded.updated_at""",
            (market, symbol, 0, first, previous_last, now if active else None, now),
        )

    conn.commit()
    return triggered


def store_technical_result(conn, scan_id, row):
    conn.execute(
        """INSERT OR REPLACE INTO technical_results(
             scan_id,market,symbol,price,rsi_1d,macd_pct_1d,rsi_4h,macd_pct_4h,
             rsi_1h,macd_pct_1h,score,evaluable,matched,alert_triggered,
             conditions_json,bar_1d,bar_4h,bar_1h,bars_1d,bars_4h,bars_1h,
             source,quality_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            int(scan_id),
            row.get("market") or "USA",
            row.get("symbol"),
            row.get("price"),
            row.get("rsi_1d"),
            row.get("macd_pct_1d"),
            row.get("rsi_4h"),
            row.get("macd_pct_4h"),
            row.get("rsi_1h"),
            row.get("macd_pct_1h"),
            int(row.get("score") or 0),
            int(bool(row.get("evaluable"))),
            int(bool(row.get("matched"))),
            int(bool(row.get("alert_triggered"))),
            json.dumps(row.get("conditions") or {}, default=str),
            row.get("bar_1d"),
            row.get("bar_4h"),
            row.get("bar_1h"),
            int(row.get("bars_1d") or 0),
            int(row.get("bars_4h") or 0),
            int(row.get("bars_1h") or 0),
            row.get("source"),
            json.dumps(row.get("quality") or {}, default=str),
        ),
    )


def latest_technical_results(conn, market=None):
    market = str(market).upper().strip() if market else None
    if market in {"USA", "ARG"}:
        sql = """
        WITH latest_run AS (
          SELECT MAX(id) AS scan_id
          FROM technical_scan_runs
          WHERE finished_at IS NOT NULL AND market=?
        )
        SELECT tr.*
        FROM technical_results tr
        JOIN latest_run lr ON lr.scan_id=tr.scan_id
        WHERE tr.market=?
        ORDER BY tr.matched DESC, tr.score DESC, tr.symbol ASC
        """
        return pd.read_sql_query(sql, conn, params=[market, market])

    sql = """
    WITH latest_runs AS (
      SELECT market, MAX(id) AS scan_id
      FROM technical_scan_runs
      WHERE finished_at IS NOT NULL
      GROUP BY market
    )
    SELECT tr.*
    FROM technical_results tr
    JOIN latest_runs lr
      ON lr.scan_id=tr.scan_id AND lr.market=tr.market
    ORDER BY tr.market, tr.matched DESC, tr.score DESC, tr.symbol ASC
    """
    return pd.read_sql_query(sql, conn)


def latest_technical_run(conn, market=None):
    market = str(market).upper().strip() if market else None
    if market in {"USA", "ARG"}:
        return pd.read_sql_query(
            """SELECT * FROM technical_scan_runs
               WHERE finished_at IS NOT NULL AND market=?
               ORDER BY id DESC LIMIT 1""",
            conn, params=[market],
        )

    return pd.read_sql_query(
        """SELECT * FROM technical_scan_runs
           WHERE finished_at IS NOT NULL
           ORDER BY id DESC LIMIT 1""",
        conn,
    )
