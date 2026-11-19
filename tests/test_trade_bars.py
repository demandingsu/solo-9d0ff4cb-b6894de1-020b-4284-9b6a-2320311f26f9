"""逐笔汇总自测：补包、撤销（先撤后到/撤极值/撤首尾）、幂等重传、
同号异值整包退回、两级汇总同快照（交错查询）、日线回写导入、旧库迁移。
"""

from __future__ import annotations

import sqlite3
import threading

from app import db as db_mod
from app.main import create_app
from fastapi.testclient import TestClient

# ---------- 辅助 ----------


def trade(no, time, price, qty):
    return {"trade_no": no, "trade_time": time, "price": price, "quantity": qty}


def packet(code="AAA", date="2026-01-02", trades=(), revokes=()):
    return {
        "security_code": code,
        "trade_date": date,
        "trades": list(trades),
        "revocations": [{"trade_no": n} for n in revokes],
    }


def post_packet(client, p):
    return client.post("/imports/trade-packets", json=p)


def get_bars(client, code="AAA", date="2026-01-02", revision=0):
    return client.get(
        "/bars",
        params={"security_code": code, "trade_date": date, "revision": revision},
    )


def minutes_by_ts(body):
    return {m["minute_ts"]: m for m in body["minute_bars"]}


# ---------- 基本汇总 ----------


def test_minute_and_daily_bars_basic(client):
    """三分钟三笔：分钟线 OHLCV 正确，日线由分钟线聚合，回写数据带指定修订号。"""
    r = post_packet(
        client,
        packet(
            trades=[
                trade(1, "2026-01-02T09:30:00Z", 10.0, 100),
                trade(2, "2026-01-02T09:30:20Z", 10.5, 200),
                trade(3, "2026-01-02T09:31:00Z", 10.2, 300),
            ]
        ),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["payload_kind"] == "trade_packet"
    assert body["applied_trades"] == 3
    assert body["row_count"] == 3
    assert body["affected_minutes"] == 2

    r = get_bars(client, revision=7)
    assert r.status_code == 200
    bars = r.json()
    mins = minutes_by_ts(bars)
    assert set(mins) == {"2026-01-02T09:30:00Z", "2026-01-02T09:31:00Z"}

    m1 = mins["2026-01-02T09:30:00Z"]
    assert (m1["open"], m1["high"], m1["low"], m1["close"]) == (10.0, 10.5, 10.0, 10.5)
    assert m1["volume"] == 300.0 and m1["trade_count"] == 2
    m2 = mins["2026-01-02T09:31:00Z"]
    assert (m2["open"], m2["high"], m2["low"], m2["close"]) == (10.2, 10.2, 10.2, 10.2)
    assert m2["volume"] == 300.0 and m2["trade_count"] == 1

    daily = bars["daily"]
    assert (daily["open"], daily["high"], daily["low"], daily["close"]) == (
        10.0, 10.5, 10.0, 10.2,
    )
    assert daily["volume"] == 600.0 and daily["trade_count"] == 3

    imp = bars["daily_quote_import"]
    assert imp == {
        "security_code": "AAA", "trade_date": "2026-01-02", "revision": 7,
        "open": 10.0, "high": 10.5, "low": 10.0, "close": 10.2, "volume": 600.0,
    }


def test_same_timestamp_tie_break_by_trade_no(client):
    """同一时刻多笔：开取成交号最小者，收取成交号最大者（与包内顺序无关）。"""
    r = post_packet(
        client,
        packet(
            trades=[
                trade(2, "2026-01-02T09:30:05Z", 11.0, 100),  # 包内先出现但成交号大
                trade(1, "2026-01-02T09:30:05Z", 10.0, 100),
                trade(3, "2026-01-02T09:30:05Z", 12.0, 100),
            ]
        ),
    )
    assert r.status_code == 201
    mins = minutes_by_ts(get_bars(client).json())
    m = mins["2026-01-02T09:30:00Z"]
    assert m["open"] == 10.0   # 成交号 1
    assert m["close"] == 12.0  # 成交号 3
    assert m["trade_count"] == 3


def test_timezone_offset_normalized_to_utc(client):
    """带偏移的成交时间归一化到 UTC 分钟，与 Z 写法同桶。"""
    r = post_packet(
        client,
        packet(
            trades=[
                trade(1, "2026-01-02T17:30:15+08:00", 10.0, 100),  # = 09:30:15Z
                trade(2, "2026-01-02T09:30:45.5Z", 11.0, 100),
            ]
        ),
    )
    assert r.status_code == 201, r.text
    mins = minutes_by_ts(get_bars(client).json())
    assert set(mins) == {"2026-01-02T09:30:00Z"}
    m = mins["2026-01-02T09:30:00Z"]
    assert m["open"] == 10.0 and m["close"] == 11.0 and m["trade_count"] == 2


# ---------- 撤销 ----------


def test_revoke_extreme_recomputes_high_low(client):
    """撤掉最高/最低价成交后，分钟线与日线极值都要改。"""
    post_packet(
        client,
        packet(
            trades=[
                trade(1, "2026-01-02T09:30:00Z", 10.0, 100),
                trade(2, "2026-01-02T09:30:30Z", 12.0, 100),  # 最高价
                trade(3, "2026-01-02T09:31:00Z", 11.0, 100),
            ]
        ),
    )
    before = get_bars(client).json()
    assert before["daily"]["high"] == 12.0

    r = post_packet(client, packet(revokes=[2]))
    assert r.status_code == 201
    assert r.json()["applied_revocations"] == 1
    assert r.json()["affected_minutes"] == 1  # 只重算 09:30 这一分钟

    after = get_bars(client).json()
    mins = minutes_by_ts(after)
    assert mins["2026-01-02T09:30:00Z"]["high"] == 10.0
    assert after["daily"]["high"] == 11.0  # 日线极值同步下降
    assert after["daily"]["volume"] == 200.0
    assert after["daily"]["trade_count"] == 2


def test_revoke_first_last_trade_changes_open_close(client):
    """撤掉首笔 -> 开盘价改；撤掉末笔 -> 收盘价改，两级同时生效。"""
    post_packet(
        client,
        packet(
            trades=[
                trade(1, "2026-01-02T09:30:00Z", 10.0, 100),
                trade(2, "2026-01-02T09:30:30Z", 11.0, 100),
                trade(3, "2026-01-02T09:31:00Z", 12.0, 100),
            ]
        ),
    )
    post_packet(client, packet(revokes=[1]))
    bars = get_bars(client).json()
    assert bars["daily"]["open"] == 11.0
    assert minutes_by_ts(bars)["2026-01-02T09:30:00Z"]["open"] == 11.0

    post_packet(client, packet(revokes=[3]))
    bars = get_bars(client).json()
    assert bars["daily"]["close"] == 11.0
    assert "2026-01-02T09:31:00Z" not in minutes_by_ts(bars)  # 分钟被清空移除


def test_revoke_empties_minute_removed_and_daily_null(client):
    """唯一一笔被撤 -> 分钟从结果移除，当日无成交 -> 日线与回写数据为 null。"""
    post_packet(client, packet(trades=[trade(1, "2026-01-02T09:30:00Z", 10.0, 100)]))
    post_packet(client, packet(revokes=[1]))
    bars = get_bars(client).json()
    assert bars["minute_bars"] == []
    assert bars["daily"] is None
    assert bars["daily_quote_import"] is None


def test_revoke_first_then_late_trade_not_counted(client):
    """撤销先到：迟到的原成交登记但不计入；重传撤销幂等。"""
    r = post_packet(client, packet(revokes=[5]))  # 撤销先到，尚无成交
    assert r.status_code == 201
    assert r.json()["applied_revocations"] == 1
    assert r.json()["affected_minutes"] == 0

    # 迟到的原成交：登记（applied_trades=1）但不触发重算、不计入
    r = post_packet(
        client, packet(trades=[trade(5, "2026-01-02T09:30:00Z", 10.0, 100)])
    )
    assert r.status_code == 201
    assert r.json()["applied_trades"] == 1
    assert r.json()["affected_minutes"] == 0
    assert get_bars(client).json()["minute_bars"] == []

    # 正常成交号不受影响
    post_packet(client, packet(trades=[trade(6, "2026-01-02T09:31:00Z", 11.0, 200)]))
    bars = get_bars(client).json()
    assert len(bars["minute_bars"]) == 1
    assert bars["daily"]["trade_count"] == 1
    assert bars["daily"]["volume"] == 200.0

    # 同包"成交+撤销"：也不计入
    post_packet(
        client,
        packet(trades=[trade(7, "2026-01-02T09:32:00Z", 12.0, 100)], revokes=[7]),
    )
    bars = get_bars(client).json()
    assert len(bars["minute_bars"]) == 1
    assert bars["daily"]["trade_count"] == 1

    # 重传撤销幂等
    r = post_packet(client, packet(revokes=[5]))
    assert r.status_code == 201 and r.json()["row_count"] == 0


# ---------- 幂等与整包退回 ----------


def test_identical_retransmit_idempotent(client):
    """原样重传不重复计量：row_count=0，分钟线/日线不变。"""
    p = packet(
        trades=[
            trade(1, "2026-01-02T09:30:00Z", 10.0, 100),
            trade(2, "2026-01-02T09:31:00Z", 11.0, 200),
        ]
    )
    r = post_packet(client, p)
    assert r.status_code == 201 and r.json()["row_count"] == 2
    before = get_bars(client).json()

    r = post_packet(client, p)  # 整包重传
    assert r.status_code == 201
    body = r.json()
    assert body["row_count"] == 0
    assert body["applied_trades"] == 0
    assert body["affected_minutes"] == 0
    assert get_bars(client).json() == before

    # 包内同号同值重复也只算一次
    p2 = packet(
        trades=[
            trade(3, "2026-01-02T09:32:00Z", 12.0, 50),
            trade(3, "2026-01-02T09:32:00Z", 12.0, 50),
        ]
    )
    r = post_packet(client, p2)
    assert r.status_code == 201 and r.json()["applied_trades"] == 1
    assert get_bars(client).json()["daily"]["trade_count"] == 3


def test_conflicting_trade_rejected_and_rolled_back(client):
    """同号成交异值：包内冲突与撞库冲突都整包退回，不留批次、不留数据。"""
    # 包内同号异值
    r = post_packet(
        client,
        packet(
            trades=[
                trade(1, "2026-01-02T09:30:00Z", 10.0, 100),
                trade(1, "2026-01-02T09:30:00Z", 10.5, 100),
            ]
        ),
    )
    assert r.status_code == 422
    assert "同号成交异值" in r.json()["detail"]
    assert client.get("/batches").json() == []

    # 正常入库一笔
    post_packet(client, packet(trades=[trade(1, "2026-01-02T09:30:00Z", 10.0, 100)]))
    before = get_bars(client).json()
    batches_before = client.get("/batches").json()

    # 撞库异值：同包另一笔本可写入，也必须一起回滚
    r = post_packet(
        client,
        packet(
            trades=[
                trade(2, "2026-01-02T09:31:00Z", 11.0, 100),
                trade(1, "2026-01-02T09:30:00Z", 99.0, 100),  # 与库中异值
            ]
        ),
    )
    assert r.status_code == 422
    assert "同号成交异值" in r.json()["detail"]
    assert client.get("/batches").json() == batches_before  # 没有新批次
    assert get_bars(client).json() == before  # 原结果保留


def test_failed_packet_keeps_previous_bars(client):
    """补包失败时保留原结果：撤销一个从未见过的成交号之外的任何失败都不留痕。"""
    post_packet(
        client,
        packet(
            trades=[
                trade(1, "2026-01-02T09:30:00Z", 10.0, 100),
                trade(2, "2026-01-02T09:31:00Z", 11.0, 200),
            ]
        ),
    )
    before = get_bars(client).json()

    # 失败包 1：同号异值
    assert post_packet(
        client, packet(trades=[trade(1, "2026-01-02T09:30:00Z", 10.9, 100)])
    ).status_code == 422
    # 失败包 2：校验错误（数量为 0）
    assert post_packet(
        client, packet(trades=[trade(9, "2026-01-02T09:33:00Z", 10.0, 0)])
    ).status_code == 422

    assert get_bars(client).json() == before


# ---------- 日线回写与两级一致性 ----------


def test_daily_quote_import_submittable(client):
    """daily_quote_import 可原样提交 /imports/quotes，并在原值查询中生效。"""
    post_packet(
        client,
        packet(
            trades=[
                trade(1, "2026-01-02T09:30:00Z", 10.0, 100),
                trade(2, "2026-01-02T09:31:00Z", 11.0, 200),
            ]
        ),
    )
    imp = get_bars(client, revision=3).json()["daily_quote_import"]
    r = client.post("/imports/quotes", json={"quotes": [imp]})
    assert r.status_code == 201, r.text
    assert r.json()["row_count"] == 1

    raw = client.get(
        "/quotes/raw",
        params={"security_code": "AAA", "start_date": "2026-01-02",
                "end_date": "2026-01-02"},
    ).json()
    assert len(raw) == 1
    assert raw[0]["revision"] == 3
    assert (raw[0]["open"], raw[0]["high"], raw[0]["low"], raw[0]["close"]) == (
        10.0, 11.0, 10.0, 11.0,
    )
    assert raw[0]["volume"] == 300.0


def test_interleaved_queries_consistent_snapshots(client):
    """交错查询：补包持续提交期间，每次响应内日线必须等于分钟线聚合。"""
    base = [
        trade(i, f"2026-01-02T09:{30 + i:02d}:00Z", 10.0 + i, 100)
        for i in range(5)
    ]
    assert post_packet(client, packet(trades=base)).status_code == 201

    stop = threading.Event()
    errors: list[Exception] = []

    def reader():
        try:
            while not stop.is_set():
                r = get_bars(client)
                assert r.status_code == 200
                body = r.json()
                mins = body["minute_bars"]
                daily = body["daily"]
                if not mins:
                    assert daily is None and body["daily_quote_import"] is None
                    continue
                # 同一响应内两级必须自洽（来自同一已提交状态）
                assert daily["open"] == mins[0]["open"]
                assert daily["close"] == mins[-1]["close"]
                assert daily["high"] == max(m["high"] for m in mins)
                assert daily["low"] == min(m["low"] for m in mins)
                assert daily["volume"] == sum(m["volume"] for m in mins)
                assert daily["trade_count"] == sum(m["trade_count"] for m in mins)
                imp = body["daily_quote_import"]
                assert imp["open"] == daily["open"]
                assert imp["close"] == daily["close"]
                assert imp["volume"] == daily["volume"]
        except Exception as exc:  # noqa: BLE001 - 并发结果需要全部收集
            errors.append(exc)

    def writer():
        for i in range(5, 15):  # 追加 09:35..09:44 各一笔
            post_packet(
                client,
                packet(trades=[trade(i, f"2026-01-02T09:{30 + i:02d}:00Z",
                                     10.0 + i, 100)]),
            )
        for no in (0, 2, 4, 6, 8):  # 再撤掉其中五笔
            post_packet(client, packet(revokes=[no]))

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    w = threading.Thread(target=writer)
    w.start()
    w.join()
    stop.set()
    for t in readers:
        t.join()

    assert errors == []
    # 最终状态确定：剩 1,3,5,7,9,10,11,12,13,14 共 10 笔
    bars = get_bars(client).json()
    assert len(bars["minute_bars"]) == 10
    assert bars["daily"]["trade_count"] == 10
    assert bars["daily"]["open"] == 11.0   # 成交号 1
    assert bars["daily"]["close"] == 24.0  # 成交号 14
    assert bars["daily"]["volume"] == 1000.0


# ---------- 校验、隔离、迁移 ----------


def test_packet_validation_errors(client):
    """空包、朴素时间、坏日期、非正价量、负成交号 => 422。"""
    assert post_packet(client, packet()).status_code == 422
    assert post_packet(
        client, packet(trades=[trade(1, "2026-01-02T09:30:00", 10.0, 100)])
    ).status_code == 422  # 缺时区
    bad_date = packet(trades=[trade(1, "2026-01-02T09:30:00Z", 10.0, 100)])
    bad_date["trade_date"] = "2026/01/02"
    assert post_packet(client, bad_date).status_code == 422
    assert post_packet(
        client, packet(trades=[trade(1, "2026-01-02T09:30:00Z", 0.0, 100)])
    ).status_code == 422
    assert post_packet(
        client, packet(trades=[trade(1, "2026-01-02T09:30:00Z", 10.0, 0)])
    ).status_code == 422
    assert post_packet(
        client, packet(trades=[trade(-1, "2026-01-02T09:30:00Z", 10.0, 100)])
    ).status_code == 422
    assert client.get("/batches").json() == []


def test_multiple_securities_isolated(client):
    """不同证券的补包与汇总互不影响。"""
    post_packet(client, packet(code="AAA",
                               trades=[trade(1, "2026-01-02T09:30:00Z", 10.0, 100)]))
    post_packet(client, packet(code="BBB",
                               trades=[trade(1, "2026-01-02T09:30:00Z", 20.0, 200)]))
    a = get_bars(client, code="AAA").json()
    b = get_bars(client, code="BBB").json()
    assert a["daily"]["close"] == 10.0 and a["daily"]["volume"] == 100.0
    assert b["daily"]["close"] == 20.0 and b["daily"]["volume"] == 200.0


def test_openapi_lists_new_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/imports/trade-packets" in paths
    assert "/bars" in paths


# 旧版建表脚本（CHECK 不含 trade_packet，无逐笔表），用于迁移自测
_OLD_SCHEMA = """
CREATE TABLE securities (
    code TEXT PRIMARY KEY, name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE ingest_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_kind TEXT NOT NULL CHECK (payload_kind IN ('quotes', 'corporate_actions')),
    row_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE daily_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    security_code TEXT NOT NULL REFERENCES securities(code),
    trade_date TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
    close REAL NOT NULL, volume REAL NOT NULL,
    ingest_batch_id INTEGER NOT NULL REFERENCES ingest_batches(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (security_code, trade_date, revision)
);
"""


def test_old_schema_migration(tmp_path, monkeypatch):
    """旧库启动时自动重建 ingest_batches：旧批次保留，补包可直接用。"""
    path = str(tmp_path / "old_market.db")
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute("INSERT INTO securities(code) VALUES ('AAA')")
    conn.execute(
        "INSERT INTO ingest_batches(payload_kind, row_count) VALUES ('quotes', 1)"
    )
    conn.execute(
        "INSERT INTO daily_quotes(security_code, trade_date, revision, open, high, "
        "low, close, volume, ingest_batch_id) "
        "VALUES ('AAA', '2026-01-02', 0, 100, 102, 98, 100, 1000, 1)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("MARKET_DB_PATH", path)
    db_mod.init_db(path)  # 触发迁移（应用启动路径会再调一次，幂等）

    app = create_app()
    with TestClient(app) as client:
        # 旧批次与旧行情原样保留
        batches = client.get("/batches").json()
        assert [b["id"] for b in batches] == [1]
        raw = client.get(
            "/quotes/raw",
            params={"security_code": "AAA", "start_date": "2026-01-02",
                    "end_date": "2026-01-02"},
        ).json()
        assert raw[0]["close"] == 100.0

        # 补包可用，批次号在旧序列上续增
        r = post_packet(
            client, packet(trades=[trade(1, "2026-01-02T09:30:00Z", 10.0, 100)])
        )
        assert r.status_code == 201
        assert r.json()["batch_id"] == 2
        bars = get_bars(client).json()
        assert bars["daily"]["close"] == 10.0
        kinds = {b["payload_kind"] for b in client.get("/batches").json()}
        assert kinds == {"quotes", "trade_packet"}
