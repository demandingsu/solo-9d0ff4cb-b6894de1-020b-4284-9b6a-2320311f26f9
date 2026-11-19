"""端到端业务流冒烟脚本（对真实 uvicorn 进程发 HTTP 请求）。

场景：
1. 导入两天行情 -> 批次1
2. 按截止批次1查询：无公告，折算值=原值
3. 导入 1拆2 公告（批次2，补报）-> 批次1 结果不变；最新批次价格减半量加倍
4. 同修订异值拒收 -> 422，无新批次
5. 高修订更正行情 -> 生效；低修订 -> 422
6. /docs 可访问
7. 逐笔补包 -> 分钟线/日线两级核对；撤极值改日线；先撤后到不计入；
   重传幂等；同号异值整包退回且原结果保留；日线回写 /imports/quotes
"""

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8077"


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        sys.exit(1)


q = lambda d, c, rev=0, vol=1000.0: {
    "security_code": "600001", "trade_date": d, "revision": rev,
    "open": c, "high": c + 2, "low": c - 2, "close": c, "volume": vol,
}

# 1. 行情批次
s, r = call("POST", "/imports/quotes",
            {"quotes": [q("2026-01-02", 100.0), q("2026-01-06", 60.0, vol=2000.0)]})
check("导入行情 201", s == 201 and r["batch_id"] == 1 and r["row_count"] == 2, r)

# 2. 无公告时查询
s, r = call("GET", "/quotes/adjusted?security_code=600001&start_date=2026-01-01&end_date=2026-01-10")
check("无公告折算=原值", s == 200 and r[0]["adjusted_close"] == 100.0 and r[0]["factor"] == 1.0)

# 3. 补报公告 1拆2，除权日 2026-01-05
s, r = call("POST", "/imports/corporate-actions", {"actions": [{
    "security_code": "600001", "event_no": "SP2026-01", "ex_date": "2026-01-05",
    "ratio": 2.0, "revision": 0, "action_type": "split"}]})
check("导入公告 201", s == 201 and r["batch_id"] == 2, r)

s, old = call("GET", "/quotes/adjusted?security_code=600001&start_date=2026-01-01&end_date=2026-01-10&cutoff_batch=1")
check("旧批次不受补报影响", old[0]["adjusted_close"] == 100.0 and old[0]["applied_events"] == [])

s, new = call("GET", "/quotes/adjusted?security_code=600001&start_date=2026-01-01&end_date=2026-01-10")
check("新批次价格减半", new[0]["adjusted_close"] == 50.0 and new[0]["factor"] == 0.5)
check("新批次量加倍", new[0]["adjusted_volume"] == 2000.0)
check("事件依据完整", new[0]["applied_events"][0]["event_no"] == "SP2026-01"
      and new[0]["applied_events"][0]["ingest_batch_id"] == 2)
check("除权日当天不折算", new[1]["trade_date"] == "2026-01-06" and new[1]["factor"] == 1.0
      and new[1]["adjusted_close"] == 60.0)

# 4. 同修订异值拒收
s, r = call("POST", "/imports/corporate-actions", {"actions": [{
    "security_code": "600001", "event_no": "SP2026-01", "ex_date": "2026-01-05",
    "ratio": 4.0, "revision": 0, "action_type": "split"}]})
check("同修订异值 422", s == 422 and "同修订异值" in r["detail"], r)

# 5. 高修订更正 + 低修订拒收
s, r = call("POST", "/imports/quotes", {"quotes": [q("2026-01-02", 110.0, rev=1)]})
check("高修订行情 201", s == 201 and r["row_count"] == 1)
s, r = call("POST", "/imports/quotes", {"quotes": [q("2026-01-02", 110.0, rev=0)]})
check("低修订 422", s == 422 and "过期修订" in r["detail"], r)

# 批次清单
s, batches = call("GET", "/batches")
check("批次清单", s == 200 and [b["id"] for b in batches] == [1, 2, 3], batches)

# ---------- 逐笔汇总（补包 -> 分钟线/日线） ----------

def pkt(trades=(), revokes=()):
    return {"security_code": "600002", "trade_date": "2026-01-02",
            "trades": list(trades),
            "revocations": [{"trade_no": n} for n in revokes]}


def tr(no, ts, price, qty):
    return {"trade_no": no, "trade_time": ts, "price": price, "quantity": qty}


# 7. 补包导入：两分钟三笔
s, r = call("POST", "/imports/trade-packets", pkt(trades=[
    tr(1, "2026-01-02T01:30:00Z", 10.0, 100),
    tr(2, "2026-01-02T01:30:20Z", 10.5, 200),
    tr(3, "2026-01-02T01:31:00Z", 10.2, 300)]))
check("补包 201", s == 201 and r["applied_trades"] == 3 and r["affected_minutes"] == 2, r)

s, bars = call("GET", "/bars?security_code=600002&trade_date=2026-01-02&revision=5")
check("分钟线两分钟", s == 200 and len(bars["minute_bars"]) == 2, bars)
check("日线聚合", bars["daily"]["open"] == 10.0 and bars["daily"]["high"] == 10.5
      and bars["daily"]["close"] == 10.2 and bars["daily"]["volume"] == 600.0, bars["daily"])
check("两级同源", bars["daily"]["open"] == bars["minute_bars"][0]["open"]
      and bars["daily"]["close"] == bars["minute_bars"][-1]["close"])
check("回写数据带修订号", bars["daily_quote_import"]["revision"] == 5
      and bars["daily_quote_import"]["close"] == 10.2, bars["daily_quote_import"])

# 8. 撤掉极值：最高价成交被撤，两级同步改
s, r = call("POST", "/imports/trade-packets", pkt(revokes=[2]))
check("撤销 201", s == 201 and r["applied_revocations"] == 1, r)
s, bars = call("GET", "/bars?security_code=600002&trade_date=2026-01-02")
check("撤极值后日线改", bars["daily"]["high"] == 10.2 and bars["daily"]["volume"] == 400.0, bars["daily"])

# 9. 先撤后到：迟到成交不计入
s, r = call("POST", "/imports/trade-packets", pkt(revokes=[9]))
check("先撤 201", s == 201, r)
s, r = call("POST", "/imports/trade-packets",
            pkt(trades=[tr(9, "2026-01-02T01:32:00Z", 99.0, 100)]))
check("迟到成交登记但不计入", s == 201 and r["affected_minutes"] == 0, r)
s, bars = call("GET", "/bars?security_code=600002&trade_date=2026-01-02")
check("迟到成交未计入", len(bars["minute_bars"]) == 2
      and bars["daily"]["trade_count"] == 2, bars["daily"])

# 10. 原样重传幂等；同号异值整包退回且原结果保留
s, r = call("POST", "/imports/trade-packets", pkt(trades=[
    tr(1, "2026-01-02T01:30:00Z", 10.0, 100)]))
check("重传幂等", s == 201 and r["row_count"] == 0, r)
s, before = call("GET", "/bars?security_code=600002&trade_date=2026-01-02")
s, r = call("POST", "/imports/trade-packets", pkt(trades=[
    tr(4, "2026-01-02T01:33:00Z", 10.4, 100),
    tr(1, "2026-01-02T01:30:00Z", 99.0, 100)]))
check("同号异值 422", s == 422 and "同号成交异值" in r["detail"], r)
s, after = call("GET", "/bars?security_code=600002&trade_date=2026-01-02")
check("失败补包保留原结果", before == after, after)

# 11. 日线回写：daily_quote_import 原样提交日行情导入
s, bars = call("GET", "/bars?security_code=600002&trade_date=2026-01-02&revision=0")
s, r = call("POST", "/imports/quotes", {"quotes": [bars["daily_quote_import"]]})
check("日线回写 201", s == 201 and r["row_count"] == 1, r)
s, raw = call("GET", "/quotes/raw?security_code=600002&start_date=2026-01-02&end_date=2026-01-02")
check("回写生效", s == 200 and raw[0]["close"] == 10.2 and raw[0]["volume"] == 400.0, raw)

# /docs
with urllib.request.urlopen(BASE + "/docs") as resp:
    check("/docs 可访问", resp.status == 200 and b"Swagger" in resp.read())

print("\nALL E2E CHECKS PASSED")
