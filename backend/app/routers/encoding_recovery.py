"""编码恢复接口：未知编码登记、失败详情、更正受理、确认恢复与重试。

路径独立于通用资源路由，避免 /api/{module} 把恢复单当成普通模块记录。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.encoding_norm import EncodingNormService
from app.services.encoding_recovery import EncodingRecoveryService, parse_byte_input
from app.store import store

router = APIRouter(prefix="/api/encoding-recovery", tags=["编码恢复"])


class AttachmentPayload(BaseModel):
    batch: str
    attachment: str = ""
    bytes_hex: str = ""
    declared_encoding: str = ""
    target_encoding: str = "UTF-8"
    strategy: str = "REPLACE"
    operator: str = "operator"
    norm_no: str = ""


class CorrectionPayload(BaseModel):
    encoding: str
    source: str = "manual"
    operator: str = "operator"
    occurred_at: str = ""


class ConfirmPayload(BaseModel):
    receipt_no: str = ""
    operator: str = "operator"
    occurred_at: str = ""


class RetryPayload(BaseModel):
    encoding: str = ""
    operator: str = "operator"
    occurred_at: str = ""


class FieldParsePayload(BaseModel):
    parse_no: str
    batch: str
    template_no: str = "ET-编码恢复"
    field_name: str
    raw_value: str = ""
    parsed_value: str = ""
    row_num: int = 1
    success: bool = True


def _service() -> EncodingRecoveryService:
    return EncodingRecoveryService()


def _require(recovery_no: str) -> dict[str, Any]:
    detail = _service().detail(recovery_no)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"恢复单 {recovery_no} 不存在")
    return detail


@router.get("/failures")
def list_failures(
    status: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items, total = _service().list_failures(status=status, keyword=keyword,
                                            page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/failures/{recovery_no}")
def failure_detail(recovery_no: str) -> dict[str, Any]:
    return _require(recovery_no)


@router.post("/attachments")
def register_attachment(payload: AttachmentPayload) -> dict[str, Any]:
    """按附件登记转码：空字节是合法空附件，未知声明转成失败编码记录。"""
    if not payload.batch.strip():
        return {"ok": False, "message": "缺少关联批次，无法登记附件", "entry": None}
    raw_bytes = parse_byte_input(payload.bytes_hex)
    norm_no = payload.norm_no.strip() or f"EN-{payload.batch}"
    ok, message, entry = EncodingNormService().record_transcode(
        norm_no=norm_no, intake_batch=payload.batch, raw_bytes=raw_bytes,
        source_encoding=payload.declared_encoding,
        target_encoding=payload.target_encoding, strategy=payload.strategy,
        elapsed_ms=1, attachment=payload.attachment, operator=payload.operator)
    result: dict[str, Any] = {
        "ok": ok, "message": message, "entry": entry,
        "附件类型": "空附件" if len(raw_bytes) == 0 else "含字节附件",
        "字节数": str(len(raw_bytes)),
    }
    if entry and entry.get("恢复编号"):
        result["恢复编号"] = entry["恢复编号"]
        result["失败编码编号"] = entry.get("失败编码编号")
        result["失败详情"] = _service().detail(entry["恢复编号"])
    return result


@router.post("/failures/{recovery_no}/corrections")
def submit_correction(recovery_no: str, payload: CorrectionPayload) -> dict[str, Any]:
    """受理更正并返回回执：页面据回执决定继续核对还是另发恢复。"""
    result = _service().submit_correction(
        recovery_no, payload.encoding, payload.source, payload.operator,
        payload.occurred_at)
    result["details"] = _service().detail(recovery_no)
    return result


@router.post("/failures/{recovery_no}/confirm")
def confirm_recovery(recovery_no: str, payload: ConfirmPayload) -> dict[str, Any]:
    result = _service().confirm_recovery(
        recovery_no, payload.operator, payload.receipt_no, payload.occurred_at)
    result["details"] = _service().detail(recovery_no)
    return result


@router.post("/failures/{recovery_no}/retry")
def retry_transcode(recovery_no: str, payload: RetryPayload) -> dict[str, Any]:
    result = _service().retry_transcode(
        recovery_no, payload.encoding, payload.operator, payload.occurred_at)
    result["details"] = _service().detail(recovery_no)
    return result


@router.get("/eligibility/{batch}")
def eligibility(batch: str) -> dict[str, Any]:
    return _service().read_eligibility(batch)


@router.post("/field-parse")
def attempt_field_parse(payload: FieldParsePayload) -> dict[str, Any]:
    """字段解析读入：凭核定转码输出与失败编码记录核验，不满足则拒绝落库。"""
    from app.services.field_parse import FieldParseService

    ok, message, entry = FieldParseService().record_parsed_field(
        payload.parse_no, payload.batch, payload.template_no,
        payload.field_name, payload.raw_value, payload.parsed_value,
        1, payload.row_num, payload.success)
    return {
        "ok": ok, "message": message, "entry": entry,
        "读入资格": _service().read_eligibility(payload.batch),
    }


@router.get("/attachments/{batch}")
def attachment_check(batch: str) -> dict[str, Any]:
    """按批次查验附件：区分未知编码失败记录、已核定空附件与已核定转码输出。"""
    failures = [
        r for r in store.rows("encoding_recovery")
        if r.get("关联批次") == batch
    ]
    norm_rows = [
        r for r in store.rows("encoding_norm")
        if r.get("关联批次") == batch
    ]
    empty = [r for r in norm_rows if r.get("转码核定") == "已核定（空附件）"]
    unknown = [r for r in failures if r.get("记录类型") == "声明无法识别"]
    return {
        "关联批次": batch,
        "未知编码记录": [
            {"恢复编号": r.get("恢复编号"), "编码声明": r.get("原始编码"),
             "原始字节数": r.get("原始字节数"), "status": r.get("status"),
             "附件名": r.get("附件名")}
            for r in unknown
        ],
        "空附件记录": [
            {"转码编号": r.get("转码编号"), "附件名": r.get("附件名"),
             "原始编码": r.get("原始编码"), "转码核定": r.get("转码核定"),
             "转码后字节": r.get("转码后字节")}
            for r in empty
        ],
        "失败编码记录数": len(failures),
        "读入资格": _service().read_eligibility(batch),
    }
