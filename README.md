# 拆股复权日行情 API

基于 **FastAPI + SQLite** 的版本化行情服务：导入带修订号的日开高低收/成交量与
拆股（并股）公告，按**入库截止批次**返回拆股前后可比较的复权日行情。
后来补报或更正的公告**不会改写此前已交付批次的序列**。

另附**逐笔汇总**：按包补传成交/撤销回报，实时重算分钟线并聚合同日日线，
供行情加工员同时核对两级汇总；仍有成交的日线可带指定修订号直接回写日行情导入。

## 业务规则

### 版本化导入（只追加，从不改写历史行）

| 规则 | 行为 |
| --- | --- |
| 同业务键（行情=证券+交易日，公告=证券+事件号）仅最高修订号生效 | 查询只取最高修订 |
| 同修订号但值不同 | **整批拒收（422）**，事务回滚，不产生批次 |
| 收到修订号低于库中已生效修订 | **整批拒收（422，"过期修订"）** |
| 同修订同值重复提交 | 幂等跳过，不写新行 |
| 每次成功导入 | 同一事务内生成一个**入库批次**（自增批次号） |

### 逐笔补包 `POST /imports/trade-packets`

一包 = **一个证券 + 一个交易日**，包内可含多条成交回报与撤销回报，整包原子生效：

| 规则 | 行为 |
| --- | --- |
| 成交回报 | 成交号 + UTC 成交时间（ISO 8601 须带时区）+ 价格 + 数量 |
| 撤销回报 | 只需成交号；撤销是**墓碑**，先撤到时迟到的原成交也**不计入** |
| 原样重传 | 幂等跳过，不重复计量（`row_count=0`） |
| 同号成交异值（包内或与库中） | **整包退回（422）**，事务回滚，**原有结果保留** |
| 成功补包 | 同一事务内写逐笔事件、重算受影响分钟、生成入库批次 |

### 分钟线 / 日线核对 `GET /bars`

- 按**未撤成交**返回该证券该交易日的分钟线（UTC 分钟升序）与当日开高低收/成交量；
- 开/收盘按成交时间取首/尾笔，**同一时刻按成交号升序**；
- 重算只扫描**受影响分钟**的逐笔明细，被清空的分钟从结果中移除；
- 日线由分钟线在同一**只读快照**内聚合：一次查询返回的两级汇总必来自
  **同一已提交状态**，读不到查询期间新提交的补包；
- 仍有成交时附 `daily_quote_import`：带**调用方指定修订号**、字段与
  `POST /imports/quotes` 完全对齐，可原样回写日行情导入接口。

### 截止批次复权查询 `GET /quotes/adjusted`

- 以请求的**区间末日 `end_date` 为基准日**；
- 只使用**截至 `cutoff_batch` 已入库**（行所在批次 ≤ 截止批次）的公告与行情；
- 对行情日 `d`，生效公告满足 `d < 除权日 ≤ end_date`（除权日当天不折算当日行情）；
- `ratio` 表示老 1 股 → 新 `ratio` 股：**1 拆 2 填 `2.0`（价格减半、成交量加倍）**，
  2 并 1 填 `0.5`（价格翻倍、成交量减半）；多笔公告比例连乘；
- 返回原值、折算值（Decimal 计算后四舍五入保留 5 位小数）、折算系数与**事件依据**
  （事件号、除权日、比例、类型、修订号、所在批次）。

### 并发与一致性

- SQLite WAL 模式 + `BEGIN IMMEDIATE` 写事务 + 30s `busy_timeout`：写事务串行，
  读不阻塞；并发导入同键时唯一索引兜底，恰好一个成功，其余整批回滚；
- 每次查询在单个只读快照事务内完成，**价量与事件依据来自同一截止批次下的已提交状态**，
  读不到查询期间新提交的批次；
- 失败导入整体回滚：不留行、不留批次；
- 逐笔事件只追加；分钟线为物化结果，随补包事务内增量重算，与事件同事务提交。

## 启动（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs ，在 Swagger UI 直接导入、查询
```

SQLite 数据文件位于命名卷 `market-data` 的 `/data/market.db`。

## 本地开发（无 Docker）

```bash
python3 -m venv --without-pip .venv
python3 -I get-pip.py                 # 如系统没有 pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python -I -m pytest -q      # 30 项自测
MARKET_DB_PATH=./data/market.db .venv/bin/python -I -m uvicorn app.main:app --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/imports/quotes` | 导入日行情，成功返回 `batch_id` |
| POST | `/imports/corporate-actions` | 导入拆股/并股公告，成功返回 `batch_id` |
| POST | `/imports/trade-packets` | 逐笔补包（一证券一交易日），成功返回 `batch_id` 与受影响分钟数 |
| GET | `/bars` | 分钟线+当日汇总（同一已提交状态），参数 `security_code/trade_date/revision` |
| GET | `/quotes/adjusted` | 复权行情（原值+折算值+事件依据），参数 `security_code/start_date/end_date/cutoff_batch` |
| GET | `/quotes/raw` | 原值日行情（截止批次下生效修订） |
| GET | `/corporate-actions` | 公告查询（每事件号生效修订） |
| GET | `/batches` | 入库批次清单 |
| GET | `/health` | 健康检查 |

### 请求示例

```bash
# 1. 导入行情（批次 1）
curl -X POST localhost:8000/imports/quotes -H 'Content-Type: application/json' -d '{
  "quotes": [{"security_code": "600001", "trade_date": "2026-01-02",
              "revision": 0, "open": 100, "high": 102, "low": 98,
              "close": 100, "volume": 1000}]}'

# 2. 补报 1 拆 2 公告（批次 2，除权日 2026-01-05）
curl -X POST localhost:8000/imports/corporate-actions -H 'Content-Type: application/json' -d '{
  "actions": [{"security_code": "600001", "event_no": "SP-001",
               "ex_date": "2026-01-05", "ratio": 2.0, "revision": 0,
               "action_type": "split"}]}'

# 3. 按批次 1 查询 -> 补报不可见，折算值=原值；不传 cutoff_batch 则取最新批次
curl 'localhost:8000/quotes/adjusted?security_code=600001&start_date=2026-01-01&end_date=2026-01-10&cutoff_batch=1'

# 4. 逐笔补包（批次 4）：两分钟三笔成交
curl -X POST localhost:8000/imports/trade-packets -H 'Content-Type: application/json' -d '{
  "security_code": "600002", "trade_date": "2026-01-02",
  "trades": [
    {"trade_no": 1, "trade_time": "2026-01-02T01:30:00Z", "price": 10.0, "quantity": 100},
    {"trade_no": 2, "trade_time": "2026-01-02T01:30:20Z", "price": 10.5, "quantity": 200},
    {"trade_no": 3, "trade_time": "2026-01-02T01:31:00Z", "price": 10.2, "quantity": 300}]}'

# 5. 撤销 2 号成交（撤掉最高价），分钟线与日线同步重算
curl -X POST localhost:8000/imports/trade-packets -H 'Content-Type: application/json' -d '{
  "security_code": "600002", "trade_date": "2026-01-02",
  "revocations": [{"trade_no": 2}]}'

# 6. 核对两级汇总；daily_quote_import 可原样回写 /imports/quotes
curl 'localhost:8000/bars?security_code=600002&trade_date=2026-01-02&revision=0'
```

## 目录结构

```
app/            FastAPI 应用（db 连接/建表、models 校验、services 业务逻辑、main 路由）
tests/          30 项 pytest 自测（复权规则、补报不可变、修订、拒收回滚、并发、
                逐笔补包/撤销/重传/两级一致性/旧库迁移）
devtools/       端到端与并发压力脚本（对真实 uvicorn 进程发 HTTP 请求）
Dockerfile / docker-compose.yml / requirements.txt
```
