"""业务逻辑：版本化导入与截止批次复权查询。

不变量
======
1. 只追加：行情/公告/逐笔事件表从不 UPDATE/DELETE，修订号决定生效版本。
2. 同键（行情=证券+交易日，公告=证券+事件号）同修订异值 => 整批拒收；
   新请求修订号低于库中已生效修订 => 整批拒收（后来补报不能覆盖旧序列）。
3. 每次 *成功* 导入在同一事务内生成一个入库批次；失败回滚，不留批次。
4. 查询在单个快照事务内完成，截止批次之后提交的任何数据都不可见。

逐笔汇总（补包）
================
* 一包 = 一个证券 + 一个交易日，包内成交/撤销回报整包原子生效；
* 撤销是墓碑：无论撤销先到还是成交先到，该成交号一律不计入汇总；
* 原样重传幂等跳过；同号成交异值 => 整包退回（422），原有结果保留；
* 分钟线物化在 minute_bars，补包事务内只扫描受影响分钟的逐笔明细重算，
  被清空的分钟整行删除；日线在查询时由分钟线聚合，两级来自同一快照。
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from . import db
from .models import (
    AdjustedQuote,
    BarsResponse,
    BatchOut,
    CorporateActionIn,
    CorporateActionOut,
    DailyBar,
    DailyQuoteImport,
    EventBasis,
    MinuteBar,
    QuoteIn,
    RawQuote,
    TradePacketIn,
    TradeReportIn,
)

# 折算值保留 5 位小数（ROUND_HALF_UP，金融惯例）
QUANT = Decimal("0.00001")


class Rejected(Exception):
    """导入被业务规则拒收（HTTP 422/409）。"""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ---------------------------------------------------------------- 批内归一化


def _check_iso_date(value: str) -> None:
    date.fromisoformat(value)


def normalize_quotes(
    rows: Iterable[QuoteIn],
) -> dict[tuple[str, str], QuoteIn]:
    """同一请求内按 (证券, 交易日) 归一化。

    同键同修订且完全相同 => 视为重复，保留一条；
    同键同修订但值不同 => 拒收；
    同键多修订 => 仅最高修订生效（其余忽略），最高修订并列且异值 => 拒收。
    """
    groups: dict[tuple[str, str], list[QuoteIn]] = {}
    for row in rows:
        groups.setdefault((row.security_code, row.trade_date), []).append(row)

    chosen: dict[tuple[str, str], QuoteIn] = {}
    for key, items in groups.items():
        by_rev: dict[int, QuoteIn] = {}
        for item in items:
            old = by_rev.get(item.revision)
            if old is not None and _quote_signature(old) != _quote_signature(item):
                raise Rejected(
                    f"同键同修订异值拒收：证券 {key[0]} 交易日 {key[1]} "
                    f"修订号 {item.revision} 在本次导入中出现不同值"
                )
            by_rev[item.revision] = item
        max_rev = max(by_rev)
        chosen[key] = by_rev[max_rev]
    return chosen


def normalize_actions(
    rows: Iterable[CorporateActionIn],
) -> dict[tuple[str, str], CorporateActionIn]:
    """同 normalize_quotes，业务键为 (证券, 事件号)。"""
    groups: dict[tuple[str, str], list[CorporateActionIn]] = {}
    for row in rows:
        groups.setdefault((row.security_code, row.event_no), []).append(row)

    chosen: dict[tuple[str, str], CorporateActionIn] = {}
    for key, items in groups.items():
        by_rev: dict[int, CorporateActionIn] = {}
        for item in items:
            old = by_rev.get(item.revision)
            if old is not None and _action_signature(old) != _action_signature(item):
                raise Rejected(
                    f"同键同修订异值拒收：证券 {key[0]} 事件号 {key[1]} "
                    f"修订号 {item.revision} 在本次导入中出现不同值"
                )
            by_rev[item.revision] = item
        chosen[key] = by_rev[max(by_rev)]
    return chosen


def _quote_signature(q: QuoteIn) -> tuple:
    return (q.open, q.high, q.low, q.close, q.volume)


def _action_signature(a: CorporateActionIn) -> tuple:
    return (a.ex_date, a.ratio, a.action_type)


# ---------------------------------------------------------------- 导入


def _ensure_securities(conn: sqlite3.Connection, codes: set[str]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO securities(code) VALUES (?)",
        [(c,) for c in sorted(codes)],
    )


def import_quotes(rows: list[QuoteIn], db_file: str | None = None) -> dict:
    normalized = normalize_quotes(rows)
    with db.get_conn(db_file) as conn:
        try:
            with db.write_transaction(conn):
                _ensure_securities(conn, {k[0] for k in normalized})

                # 先建批次，行数据再引用它；全部在同一事务内
                cur = conn.execute(
                    "INSERT INTO ingest_batches(payload_kind, row_count) "
                    "VALUES ('quotes', 0)"
                )
                batch_id = cur.lastrowid

                inserted = 0
                for (code, tdate), row in sorted(normalized.items()):
                    existing = conn.execute(
                        "SELECT revision, open, high, low, close, volume "
                        "FROM daily_quotes WHERE security_code = ? AND trade_date = ? "
                        "ORDER BY revision DESC LIMIT 1",
                        (code, tdate),
                    ).fetchone()

                    if existing is not None:
                        if existing["revision"] > row.revision:
                            raise Rejected(
                                f"过期修订拒收：证券 {code} 交易日 {tdate} 库中已生效 "
                                f"修订号 {existing['revision']}，收到 {row.revision}"
                            )
                        if existing["revision"] == row.revision:
                            sig = (
                                existing["open"],
                                existing["high"],
                                existing["low"],
                                existing["close"],
                                existing["volume"],
                            )
                            if sig == _quote_signature(row):
                                continue  # 完全重复：幂等跳过
                            raise Rejected(
                                f"同修订异值拒收：证券 {code} 交易日 {tdate} "
                                f"修订号 {row.revision} 已存在不同值"
                            )

                    conn.execute(
                        "INSERT INTO daily_quotes(security_code, trade_date, revision, "
                        "open, high, low, close, volume, ingest_batch_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            code,
                            tdate,
                            row.revision,
                            row.open,
                            row.high,
                            row.low,
                            row.close,
                            row.volume,
                            batch_id,
                        ),
                    )
                    inserted += 1

                conn.execute(
                    "UPDATE ingest_batches SET row_count = ? WHERE id = ?",
                    (inserted, batch_id),
                )
        except sqlite3.IntegrityError as exc:
            # 唯一索引兜底：并发导入同一 (键, 修订) 时其中一个事务在此失败
            raise Rejected(f"导入与已入库数据冲突，整批拒收：{exc}", 409) from exc

    return {
        "batch_id": batch_id,
        "payload_kind": "quotes",
        "row_count": inserted,
        "received": len(rows),
    }


def import_actions(
    rows: list[CorporateActionIn], db_file: str | None = None
) -> dict:
    normalized = normalize_actions(rows)
    with db.get_conn(db_file) as conn:
        try:
            with db.write_transaction(conn):
                _ensure_securities(conn, {k[0] for k in normalized})

                cur = conn.execute(
                    "INSERT INTO ingest_batches(payload_kind, row_count) "
                    "VALUES ('corporate_actions', 0)"
                )
                batch_id = cur.lastrowid

                inserted = 0
                for (code, event_no), row in sorted(normalized.items()):
                    existing = conn.execute(
                        "SELECT revision, ex_date, ratio, action_type "
                        "FROM corporate_actions WHERE security_code = ? AND event_no = ? "
                        "ORDER BY revision DESC LIMIT 1",
                        (code, event_no),
                    ).fetchone()

                    if existing is not None:
                        if existing["revision"] > row.revision:
                            raise Rejected(
                                f"过期修订拒收：证券 {code} 事件 {event_no} 库中已生效 "
                                f"修订号 {existing['revision']}，收到 {row.revision}"
                            )
                        if existing["revision"] == row.revision:
                            sig = (
                                existing["ex_date"],
                                existing["ratio"],
                                existing["action_type"],
                            )
                            if sig == _action_signature(row):
                                continue
                            raise Rejected(
                                f"同修订异值拒收：证券 {code} 事件 {event_no} "
                                f"修订号 {row.revision} 已存在不同值"
                            )

                    conn.execute(
                        "INSERT INTO corporate_actions(security_code, event_no, ex_date, "
                        "ratio, action_type, revision, ingest_batch_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            code,
                            event_no,
                            row.ex_date,
                            row.ratio,
                            row.action_type,
                            row.revision,
                            batch_id,
                        ),
                    )
                    inserted += 1

                conn.execute(
                    "UPDATE ingest_batches SET row_count = ? WHERE id = ?",
                    (inserted, batch_id),
                )
        except sqlite3.IntegrityError as exc:
            raise Rejected(f"导入与已入库数据冲突，整批拒收：{exc}", 409) from exc

    return {
        "batch_id": batch_id,
        "payload_kind": "corporate_actions",
        "row_count": inserted,
        "received": len(rows),
    }


# ---------------------------------------------------------------- 逐笔补包


def _minute_of(trade_time: str) -> str:
    """归一化 UTC 时间 -> 所在分钟（YYYY-MM-DDTHH:MM:00Z）。"""
    return trade_time[:16] + ":00Z"


def _trade_signature(t: TradeReportIn) -> tuple:
    return (t.trade_time, t.price, t.quantity)


def import_trade_packet(packet: TradePacketIn, db_file: str | None = None) -> dict:
    """补包导入：整包原子生效，失败回滚、原有分钟线/日线结果保留。

    规则：同号成交包内异值或与库中异值 => 整包退回；原样重传幂等跳过；
    撤销是墓碑（先撤后到也不计入）；只重算受影响分钟。
    """
    # 包内归一化：同号成交同值去重、异值整包退回；撤销只需成交号，去重即可
    trades: dict[int, TradeReportIn] = {}
    for t in packet.trades:
        old = trades.get(t.trade_no)
        if old is not None:
            if _trade_signature(old) != _trade_signature(t):
                raise Rejected(
                    f"同号成交异值拒收：成交号 {t.trade_no} 在包内出现不同值，整包退回"
                )
            continue
        trades[t.trade_no] = t
    revoke_nos = {r.trade_no for r in packet.revocations}

    code, tdate = packet.security_code, packet.trade_date
    with db.get_conn(db_file) as conn:
        try:
            with db.write_transaction(conn):
                _ensure_securities(conn, {code})
                cur = conn.execute(
                    "INSERT INTO ingest_batches(payload_kind, row_count) "
                    "VALUES ('trade_packet', 0)"
                )
                batch_id = cur.lastrowid

                affected: set[str] = set()
                applied_trades = 0
                new_trade_nos: set[int] = set()
                for trade_no, t in sorted(trades.items()):
                    row = conn.execute(
                        "SELECT trade_time, price, quantity FROM trade_events "
                        "WHERE security_code = ? AND trade_date = ? "
                        "AND trade_no = ? AND kind = 'trade'",
                        (code, tdate, trade_no),
                    ).fetchone()
                    if row is not None:
                        if (row["trade_time"], row["price"], row["quantity"]) != (
                            _trade_signature(t)
                        ):
                            raise Rejected(
                                f"同号成交异值拒收：证券 {code} 交易日 {tdate} "
                                f"成交号 {trade_no} 已入库值与本次不同，整包退回"
                            )
                        continue  # 原样重传：幂等跳过，不重复计量
                    conn.execute(
                        "INSERT INTO trade_events(security_code, trade_date, trade_no, "
                        "kind, trade_time, price, quantity, minute_ts, ingest_batch_id) "
                        "VALUES (?, ?, ?, 'trade', ?, ?, ?, ?, ?)",
                        (
                            code, tdate, trade_no, t.trade_time, t.price, t.quantity,
                            _minute_of(t.trade_time), batch_id,
                        ),
                    )
                    applied_trades += 1
                    new_trade_nos.add(trade_no)
                    # 撤销先到（库中已有墓碑）或同包即撤：该成交不计入
                    if trade_no in revoke_nos:
                        continue
                    revoked = conn.execute(
                        "SELECT 1 FROM trade_events WHERE security_code = ? "
                        "AND trade_date = ? AND trade_no = ? AND kind = 'revoke'",
                        (code, tdate, trade_no),
                    ).fetchone()
                    if revoked is None:
                        affected.add(_minute_of(t.trade_time))

                applied_revokes = 0
                for trade_no in sorted(revoke_nos):
                    exists = conn.execute(
                        "SELECT 1 FROM trade_events WHERE security_code = ? "
                        "AND trade_date = ? AND trade_no = ? AND kind = 'revoke'",
                        (code, tdate, trade_no),
                    ).fetchone()
                    if exists:
                        continue  # 重复撤销：幂等跳过
                    conn.execute(
                        "INSERT INTO trade_events(security_code, trade_date, trade_no, "
                        "kind, ingest_batch_id) VALUES (?, ?, ?, 'revoke', ?)",
                        (code, tdate, trade_no, batch_id),
                    )
                    applied_revokes += 1
                    if trade_no in new_trade_nos:
                        continue  # 本包新成交随即被撤：从未计入，分钟不受影响
                    tr = conn.execute(
                        "SELECT minute_ts FROM trade_events WHERE security_code = ? "
                        "AND trade_date = ? AND trade_no = ? AND kind = 'trade'",
                        (code, tdate, trade_no),
                    ).fetchone()
                    if tr is not None:
                        # 此前计入的成交被撤 => 其分钟受影响（撤销先到无成交行则无需重算）
                        affected.add(tr["minute_ts"])

                _recompute_minutes(conn, code, tdate, affected, batch_id)
                conn.execute(
                    "UPDATE ingest_batches SET row_count = ? WHERE id = ?",
                    (applied_trades + applied_revokes, batch_id),
                )
        except sqlite3.IntegrityError as exc:
            # 唯一索引兜底：并发补同一 (键, 类别) 时其中一个事务在此失败
            raise Rejected(f"补包与已入库数据冲突，整包退回：{exc}", 409) from exc

    return {
        "batch_id": batch_id,
        "payload_kind": "trade_packet",
        "row_count": applied_trades + applied_revokes,
        "received": len(packet.trades) + len(packet.revocations),
        "applied_trades": applied_trades,
        "applied_revocations": applied_revokes,
        "affected_minutes": len(affected),
    }


def _recompute_minutes(
    conn: sqlite3.Connection,
    code: str,
    tdate: str,
    minutes: set[str],
    batch_id: int,
) -> None:
    """只扫描受影响分钟的逐笔明细重算分钟线；无未撤成交的分钟整行删除。

    开/收 = 该分钟未撤成交按 (成交时间, 成交号) 升序的首/尾笔价格。
    """
    for minute in sorted(minutes):
        rows = conn.execute(
            """
            SELECT t.trade_time, t.trade_no, t.price, t.quantity
            FROM trade_events t
            WHERE t.security_code = ? AND t.trade_date = ?
              AND t.kind = 'trade' AND t.minute_ts = ?
              AND NOT EXISTS (
                  SELECT 1 FROM trade_events r
                  WHERE r.security_code = t.security_code
                    AND r.trade_date = t.trade_date
                    AND r.trade_no = t.trade_no
                    AND r.kind = 'revoke'
              )
            ORDER BY t.trade_time, t.trade_no
            """,
            (code, tdate, minute),
        ).fetchall()
        if not rows:
            conn.execute(
                "DELETE FROM minute_bars WHERE security_code = ? "
                "AND trade_date = ? AND minute_ts = ?",
                (code, tdate, minute),
            )
            continue
        prices = [r["price"] for r in rows]
        conn.execute(
            """
            INSERT INTO minute_bars(security_code, trade_date, minute_ts,
                                    open, high, low, close, volume, trade_count,
                                    updated_batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(security_code, trade_date, minute_ts) DO UPDATE SET
                open = excluded.open, high = excluded.high, low = excluded.low,
                close = excluded.close, volume = excluded.volume,
                trade_count = excluded.trade_count,
                updated_batch_id = excluded.updated_batch_id
            """,
            (
                code, tdate, minute,
                rows[0]["price"], max(prices), min(prices), rows[-1]["price"],
                math.fsum(r["quantity"] for r in rows), len(rows), batch_id,
            ),
        )


def query_bars(
    code: str,
    tdate: str,
    revision: int = 0,
    db_file: str | None = None,
) -> BarsResponse:
    """分钟线 + 当日汇总，同一只读快照内取数，两级必来自同一已提交状态。

    日线由分钟线聚合：开=最早分钟开、收=最晚分钟收、高/低=分钟极值、量=求和。
    仍有未撤成交时，按调用方指定修订号给出可直接提交 /imports/quotes 的日线。
    """
    _check_iso_date(tdate)
    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        rows = conn.execute(
            "SELECT minute_ts, open, high, low, close, volume, trade_count "
            "FROM minute_bars WHERE security_code = ? AND trade_date = ? "
            "ORDER BY minute_ts",
            (code, tdate),
        ).fetchall()
        minutes = [MinuteBar(**dict(r)) for r in rows]

        daily: DailyBar | None = None
        daily_import: DailyQuoteImport | None = None
        if minutes:
            daily = DailyBar(
                open=minutes[0].open,
                high=max(m.high for m in minutes),
                low=min(m.low for m in minutes),
                close=minutes[-1].close,
                volume=math.fsum(m.volume for m in minutes),
                trade_count=sum(m.trade_count for m in minutes),
            )
            daily_import = DailyQuoteImport(
                security_code=code,
                trade_date=tdate,
                revision=revision,
                open=daily.open,
                high=daily.high,
                low=daily.low,
                close=daily.close,
                volume=daily.volume,
            )
    return BarsResponse(
        security_code=code,
        trade_date=tdate,
        minute_bars=minutes,
        daily=daily,
        daily_quote_import=daily_import,
    )


# ---------------------------------------------------------------- 查询


def list_batches(db_file: str | None = None) -> list[BatchOut]:
    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        rows = conn.execute(
            "SELECT id, payload_kind, row_count, imported_at "
            "FROM ingest_batches ORDER BY id"
        ).fetchall()
    return [BatchOut(**dict(r)) for r in rows]


def _resolve_cutoff(conn: sqlite3.Connection, cutoff: int | None) -> int:
    row = conn.execute("SELECT MAX(id) AS m FROM ingest_batches").fetchone()
    latest = row["m"]
    if latest is None:
        raise Rejected("库中尚无任何入库批次", 404)
    if cutoff is None:
        return int(latest)
    exists = conn.execute(
        "SELECT 1 FROM ingest_batches WHERE id = ?", (cutoff,)
    ).fetchone()
    if exists is None:
        raise Rejected(f"截止批次 {cutoff} 不存在", 404)
    return cutoff


def _effective_quotes(
    conn: sqlite3.Connection, code: str, start: str, end: str, cutoff: int
) -> list[sqlite3.Row]:
    """截至 cutoff 每个交易日的生效修订（同键最高修订号）。"""
    return conn.execute(
        """
        SELECT q.security_code, q.trade_date, q.revision,
               q.open, q.high, q.low, q.close, q.volume, q.ingest_batch_id
        FROM daily_quotes q
        JOIN (
            SELECT security_code, trade_date, MAX(revision) AS max_rev
            FROM daily_quotes
            WHERE ingest_batch_id <= ?
            GROUP BY security_code, trade_date
        ) m ON m.security_code = q.security_code
           AND m.trade_date = q.trade_date
           AND q.revision = m.max_rev
        WHERE q.ingest_batch_id <= ?
          AND q.security_code = ?
          AND q.trade_date BETWEEN ? AND ?
        ORDER BY q.trade_date
        """,
        (cutoff, cutoff, code, start, end),
    ).fetchall()


def _effective_actions_for_day(
    conn: sqlite3.Connection, code: str, trade_date: str, end: str, cutoff: int
) -> list[sqlite3.Row]:
    """对某行情日生效的公告：除权日晚于行情日、不晚于基准日，且截至 cutoff 已入库。"""
    return conn.execute(
        """
        SELECT a.event_no, a.ex_date, a.ratio, a.action_type,
               a.revision, a.ingest_batch_id
        FROM corporate_actions a
        JOIN (
            SELECT security_code, event_no, MAX(revision) AS max_rev
            FROM corporate_actions
            WHERE ingest_batch_id <= ?
            GROUP BY security_code, event_no
        ) m ON m.security_code = a.security_code
           AND m.event_no = a.event_no
           AND a.revision = m.max_rev
        WHERE a.ingest_batch_id <= ?
          AND a.security_code = ?
          AND a.ex_date > ?
          AND a.ex_date <= ?
        ORDER BY a.ex_date, a.event_no
        """,
        (cutoff, cutoff, code, trade_date, end),
    ).fetchall()


def _quantize(value: Decimal) -> float:
    return float(value.quantize(QUANT, rounding=ROUND_HALF_UP))


def query_adjusted_quotes(
    code: str,
    start: str,
    end: str,
    cutoff: int | None = None,
    db_file: str | None = None,
) -> list[AdjustedQuote]:
    """按截止批次与日期区间返回原值、折算值及事件依据。

    基准日固定为区间末日 end：仅使用 ``trade_date < ex_date <= end`` 的公告。
    拆成两股（ratio=2）价格减半、成交量加倍；多笔公告比例连乘。
    """
    _check_iso_date(start)
    _check_iso_date(end)
    if start > end:
        raise Rejected("start_date 不能晚于 end_date")

    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        cutoff_id = _resolve_cutoff(conn, cutoff)
        quote_rows = _effective_quotes(conn, code, start, end, cutoff_id)

        results: list[AdjustedQuote] = []
        for q in quote_rows:
            action_rows = _effective_actions_for_day(
                conn, code, q["trade_date"], end, cutoff_id
            )
            # 系数用 Decimal 计算，避免浮点连乘误差
            factor_dec = Decimal(1)
            for a in action_rows:
                factor_dec /= Decimal(str(a["ratio"]))

            events = [
                EventBasis(
                    event_no=a["event_no"],
                    ex_date=a["ex_date"],
                    ratio=a["ratio"],
                    action_type=a["action_type"],
                    revision=a["revision"],
                    ingest_batch_id=a["ingest_batch_id"],
                )
                for a in action_rows
            ]

            def adj_price(x: float) -> float:
                return _quantize(Decimal(str(x)) * factor_dec)

            def adj_volume(x: float) -> float:
                # 量的折算 = 原值 / 系数 = 原值 * 比例之积
                return _quantize(Decimal(str(x)) / factor_dec)

            results.append(
                AdjustedQuote(
                    security_code=q["security_code"],
                    trade_date=q["trade_date"],
                    open=q["open"],
                    high=q["high"],
                    low=q["low"],
                    close=q["close"],
                    volume=q["volume"],
                    adjusted_open=adj_price(q["open"]),
                    adjusted_high=adj_price(q["high"]),
                    adjusted_low=adj_price(q["low"]),
                    adjusted_close=adj_price(q["close"]),
                    adjusted_volume=adj_volume(q["volume"]),
                    factor=_quantize(factor_dec),
                    applied_events=events,
                    cutoff_batch_id=cutoff_id,
                    basis_end_date=end,
                )
            )
    return results


def query_raw_quotes(
    code: str,
    start: str,
    end: str,
    cutoff: int | None = None,
    db_file: str | None = None,
) -> list[RawQuote]:
    """原值查询（生效修订），便于核对。"""
    _check_iso_date(start)
    _check_iso_date(end)
    if start > end:
        raise Rejected("start_date 不能晚于 end_date")

    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        cutoff_id = _resolve_cutoff(conn, cutoff)
        rows = _effective_quotes(conn, code, start, end, cutoff_id)
    return [
        RawQuote(
            security_code=r["security_code"],
            trade_date=r["trade_date"],
            revision=r["revision"],
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=r["volume"],
            ingest_batch_id=r["ingest_batch_id"],
        )
        for r in rows
    ]


def query_actions(
    code: str,
    cutoff: int | None = None,
    db_file: str | None = None,
) -> list[CorporateActionOut]:
    """截至某批次已生效的公告（每事件号最高修订）。"""
    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        cutoff_id = _resolve_cutoff(conn, cutoff)
        rows = conn.execute(
            """
            SELECT a.security_code, a.event_no, a.ex_date, a.ratio,
                   a.action_type, a.revision, a.ingest_batch_id
            FROM corporate_actions a
            JOIN (
                SELECT security_code, event_no, MAX(revision) AS max_rev
                FROM corporate_actions
                WHERE ingest_batch_id <= ?
                GROUP BY security_code, event_no
            ) m ON m.security_code = a.security_code
               AND m.event_no = a.event_no
               AND a.revision = m.max_rev
            WHERE a.ingest_batch_id <= ? AND a.security_code = ?
            ORDER BY a.ex_date, a.event_no
            """,
            (cutoff_id, cutoff_id, code),
        ).fetchall()
    return [CorporateActionOut(**dict(r)) for r in rows]
