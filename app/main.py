"""FastAPI 应用入口。

启动后访问 ``/docs`` 即可在 Swagger UI 中导入与查询。
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from . import db, services
from .models import (
    AdjustedQuote,
    BarsResponse,
    BatchOut,
    CorporateActionImportRequest,
    CorporateActionOut,
    ImportResponse,
    QuoteImportRequest,
    RawQuote,
    TradePacketImportResponse,
    TradePacketIn,
)
from .services import Rejected


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.init_db()
        yield

    app = FastAPI(
        title="拆股复权行情 API",
        version="1.1.0",
        description=(
            "版本化日行情与拆股/并股公告导入，按入库截止批次返回"
            "前后可比的复权价量。**后来补报的公告不会改写旧批次结果。**\n\n"
            "逐笔汇总：按包（一证券一交易日）补传成交/撤销回报，"
            "实时重算分钟线并聚合同日日线，供行情加工员两级核对。"
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(Rejected)
    async def _rejected_handler(_request, exc: Rejected) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.message}
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def _locked_handler(_request, exc: sqlite3.OperationalError) -> JSONResponse:
        # busy_timeout 耗尽（理论上 30s 内串行写不会触发）
        return JSONResponse(
            status_code=503,
            content={"detail": f"数据库繁忙，请重试：{exc}"},
        )

    @app.get("/health", tags=["运维"])
    def health() -> dict:
        return {"status": "ok"}

    # ---------- 导入 ----------

    @app.post(
        "/imports/quotes",
        response_model=ImportResponse,
        status_code=201,
        tags=["导入"],
        summary="导入日开高低收与成交量（带修订号，成功后生成入库批次）",
    )
    def import_quotes(req: QuoteImportRequest) -> ImportResponse:
        result = services.import_quotes(req.quotes)
        return ImportResponse(**result)

    @app.post(
        "/imports/corporate-actions",
        response_model=ImportResponse,
        status_code=201,
        tags=["导入"],
        summary="导入拆股/并股公告（带事件号、除权日、比例、修订号）",
    )
    def import_actions(req: CorporateActionImportRequest) -> ImportResponse:
        result = services.import_actions(req.actions)
        return ImportResponse(**result)

    @app.post(
        "/imports/trade-packets",
        response_model=TradePacketImportResponse,
        status_code=201,
        tags=["逐笔汇总"],
        summary="补包导入逐笔成交/撤销回报（一包 = 一个证券 + 一个交易日）",
        description=(
            "成交回报带成交号、UTC 成交时间、价格、数量；撤销回报只需成交号。"
            "撤销是墓碑：先撤到时迟到的原成交也不计入。原样重传幂等；"
            "同号成交异值整包退回（422），原有结果保留。"
            "成功后在同一事务内重算受影响分钟并生成入库批次。"
        ),
    )
    def import_trade_packet(req: TradePacketIn) -> TradePacketImportResponse:
        result = services.import_trade_packet(req)
        return TradePacketImportResponse(**result)

    # ---------- 查询 ----------

    @app.get(
        "/batches",
        response_model=list[BatchOut],
        tags=["查询"],
        summary="列出全部入库批次（批次号即截止点）",
    )
    def list_batches() -> list[BatchOut]:
        return services.list_batches()

    @app.get(
        "/quotes/adjusted",
        response_model=list[AdjustedQuote],
        tags=["查询"],
        summary="复权日行情：原值 + 折算值 + 事件依据",
        description=(
            "以 `end_date` 为基准日，只使用截至 `cutoff_batch` 已入库、"
            "除权日晚于行情日且不晚于基准日的公告折算早期价量。"
            "比例 ratio 表示老 1 股 -> 新 ratio 股（1 拆 2 填 2.0，"
            "价格减半、成交量加倍；2 并 1 填 0.5）。"
        ),
    )
    def adjusted_quotes(
        security_code: str = Query(..., description="证券代码"),
        start_date: str = Query(..., description="区间起日 YYYY-MM-DD"),
        end_date: str = Query(..., description="区间末日（复权基准日）YYYY-MM-DD"),
        cutoff_batch: int | None = Query(
            None, description="截止批次号；缺省=当前最新已提交批次"
        ),
    ) -> list[AdjustedQuote]:
        return services.query_adjusted_quotes(
            security_code, start_date, end_date, cutoff_batch
        )

    @app.get(
        "/quotes/raw",
        response_model=list[RawQuote],
        tags=["查询"],
        summary="原值日行情查询（截止批次下的生效修订）",
    )
    def raw_quotes(
        security_code: str,
        start_date: str,
        end_date: str,
        cutoff_batch: int | None = Query(None),
    ) -> list[RawQuote]:
        return services.query_raw_quotes(
            security_code, start_date, end_date, cutoff_batch
        )

    @app.get(
        "/corporate-actions",
        response_model=list[CorporateActionOut],
        tags=["查询"],
        summary="公告查询（截止批次下每事件号的生效修订）",
    )
    def actions(
        security_code: str,
        cutoff_batch: int | None = Query(None),
    ) -> list[CorporateActionOut]:
        return services.query_actions(security_code, cutoff_batch)

    @app.get(
        "/bars",
        response_model=BarsResponse,
        tags=["逐笔汇总"],
        summary="分钟线 + 当日汇总核对查询（两级来自同一已提交状态）",
        description=(
            "按未撤成交返回该证券该交易日的分钟线（UTC 分钟升序）与当日"
            "开高低收/成交量；被清空的分钟不出现在结果中。仍有成交时附 "
            "`daily_quote_import`：带调用方指定修订号、可直接提交 "
            "`POST /imports/quotes` 的日线数据。"
        ),
    )
    def bars(
        security_code: str = Query(..., description="证券代码"),
        trade_date: str = Query(..., description="交易日 YYYY-MM-DD"),
        revision: int = Query(
            0, ge=0, description="写回 daily_quote_import 的修订号（调用方指定）"
        ),
    ) -> BarsResponse:
        return services.query_bars(security_code, trade_date, revision)

    return app


app = create_app()
