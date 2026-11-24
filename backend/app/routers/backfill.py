"""事件补录路由：补录文件导入、逐行核对、确认、拒收文件下载与覆盖人数发布。"""
from __future__ import annotations

import io
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.event_backfill import (
    POLICY_HOLD, POLICY_PARTIAL, backfill_service,
)

router = APIRouter(prefix="/api/backfill", tags=["事件补录"])


class BackfillImportPayload(BaseModel):
    content: str = Field(description="CSV 补录文件全文")
    source_org: str
    source_file: str
    shift_code: str = ""
    batch_no: str | None = None


class ConfirmLinePayload(BaseModel):
    region: str | None = None


class ConfirmBatchPayload(BaseModel):
    region: str


class EstimatePayload(BaseModel):
    shift_code: str
    window_start: str
    window_end: str
    policy: str = POLICY_PARTIAL
    baseline_count: int | None = None


class LedgerPayload(BaseModel):
    shift_code: str
    window_start: str
    window_end: str
    estimate_id: int | None = None


class BaselinePayload(BaseModel):
    shift_code: str
    baseline_count: int
    window_start: str
    window_end: str
    note: str = "已确认旧估算"


@router.post("/batches/import")
def import_backfill(payload: BackfillImportPayload) -> dict[str, Any]:
    """导入外部机构补录文件，逐行核对后返回实际接收批次（含拒收行文件路径）。"""
    try:
        batch = backfill_service.import_backfill_file(
            payload.content,
            source_org=payload.source_org,
            source_file=payload.source_file,
            shift_code=payload.shift_code,
            batch_no=payload.batch_no,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "batch": batch}


@router.get("/batches")
def list_batches(shift_code: str | None = None) -> dict[str, Any]:
    items = backfill_service.list_batches(shift_code)
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int) -> dict[str, Any]:
    try:
        return {"ok": True, "batch": backfill_service.get_batch(batch_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/batches/{batch_id}/rejected-file")
def download_reject_file(batch_id: int) -> StreamingResponse:
    content = backfill_service.get_reject_file_content(batch_id)
    if not content:
        raise HTTPException(status_code=404, detail="该批次没有拒收/待核行文件")
    buffer = io.BytesIO(content.encode("utf-8"))
    return StreamingResponse(
        buffer,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="batch_{batch_id}_rejected.csv"'},
    )


@router.post("/batches/{batch_id}/lines/{line_no}/confirm")
def confirm_line(batch_id: int, line_no: int, payload: ConfirmLinePayload) -> dict[str, Any]:
    result = backfill_service.confirm_line(batch_id, line_no, region=payload.region)
    result["batch"] = backfill_service.get_batch(batch_id)
    return result


@router.post("/batches/{batch_id}/confirm-region")
def confirm_batch_region(batch_id: int, payload: ConfirmBatchPayload) -> dict[str, Any]:
    return backfill_service.confirm_batch_region(batch_id, payload.region)


@router.get("/coverage/window")
def window_coverage(window_start: str, window_end: str,
                    region: str | None = None) -> dict[str, Any]:
    """实时聚合：按实际接收的补录事件计算窗口覆盖人数及采用的文件行号。"""
    return {"ok": True, "coverage": backfill_service.window_coverage(
        window_start, window_end, region=region)}


@router.post("/estimates")
def publish_estimate(payload: EstimatePayload) -> dict[str, Any]:
    if payload.policy not in (POLICY_PARTIAL, POLICY_HOLD):
        raise HTTPException(status_code=400, detail=f"口径仅支持 {POLICY_PARTIAL} / {POLICY_HOLD}")
    estimate = backfill_service.publish_city_estimate(
        shift_code=payload.shift_code,
        window_start=payload.window_start,
        window_end=payload.window_end,
        policy=payload.policy,
        baseline_count=payload.baseline_count,
    )
    return {"ok": True, "estimate": estimate}


@router.get("/estimates")
def list_estimates(shift_code: str | None = None) -> dict[str, Any]:
    items = backfill_service.list_estimates(shift_code)
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/estimates/latest")
def latest_estimate(shift_code: str) -> dict[str, Any]:
    estimate = backfill_service.latest_estimate(shift_code)
    if estimate is None:
        return {"ok": True, "estimate": None}
    return {"ok": True, "estimate": estimate}


@router.post("/ledgers")
def publish_ledgers(payload: LedgerPayload) -> dict[str, Any]:
    return backfill_service.publish_regional_ledgers(
        shift_code=payload.shift_code,
        window_start=payload.window_start,
        window_end=payload.window_end,
        estimate_id=payload.estimate_id,
    )


@router.get("/ledgers")
def list_ledgers(shift_code: str | None = None) -> dict[str, Any]:
    items = backfill_service.list_ledgers(shift_code)
    return {"ok": True, "items": items, "total": len(items)}


@router.post("/baseline")
def register_baseline(payload: BaselinePayload) -> dict[str, Any]:
    """登记已确认旧估算基线（例如本班旧估算 30 人）。"""
    estimate = backfill_service.register_baseline(
        shift_code=payload.shift_code,
        baseline_count=payload.baseline_count,
        window_start=payload.window_start,
        window_end=payload.window_end,
        note=payload.note,
    )
    return {"ok": True, "estimate": estimate}
