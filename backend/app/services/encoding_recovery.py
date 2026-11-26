"""编码恢复领域服务：保留转码失败的原字节与编码声明，按受理回执恢复并产出恢复数据集。

设计要点：
- 无法识别的声明与真正空附件分开登记：前者进失败编码记录，后者按合法空输入核定。
- 失败记录永久保留当时的编码声明、原始字节和原错误；后续更正只追加受理回执。
- 渠道更正优先于人工选码；两者同时存在时由回执明确说明本次受理了哪一条，
  未被采用的更正另发恢复单，绝不覆盖已受理的那一条。
- 恢复后的字段写入按“恢复编号 + 行号 + 字段名”去重，重复确认只写入一次。
"""
from __future__ import annotations

import hashlib
import threading
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 候选解码编码：只保留能成功解码的，结果指纹不同的才算真正不同的更正。
CANDIDATE_ENCODINGS = (
    "UTF-8", "GB18030", "GBK", "BIG5", "Shift_JIS", "EUC-JP", "ISO-8859-1",
)

SOURCE_ALIASES = {
    "channel-correction": "渠道更正",
    "渠道更正": "渠道更正",
    "channel": "渠道更正",
    "manual": "人工选码",
    "manual-encoding": "人工选码",
    "人工选码": "人工选码",
    "operator": "人工选码",
}

SOURCE_RANK = {"人工选码": 1, "渠道更正": 2}

UNKNOWN_TOKENS = {
    "unknown", "unknown-charset", "unknown_charset", "unknown-encoding",
    "auto", "detect", "auto-detect", "none", "未知", "未知编码", "无法识别",
}

RECOVERY_TABLE = "encoding_recovery"
# 内存仓库没有事务；确认恢复与登记失败需要串行化，避免并发重复写入字段。
_RECOVERY_LOCK = threading.RLock()
FIELD_TABLE = "field_parse"
LEDGER_TABLE = "intake_audit"


def byte_length(raw_bytes: bytes) -> str:
    return str(len(raw_bytes))


def parse_byte_input(value: Any) -> bytes:
    """把界面上的字节输入转成 bytes：全十六进制按十六进制读，否则按 UTF-8 文本读。

    空输入就是合法的空附件（0 字节），不会被当成缺失字段。
    """
    text = str(value or "")
    compact = "".join(text.split())
    if compact.startswith("0x"):
        compact = compact[2:]
    if compact and len(compact) % 2 == 0 and all(
            ch in "0123456789abcdefABCDEF" for ch in compact):
        try:
            return bytes.fromhex(compact)
        except ValueError:
            pass
    return text.encode("utf-8")


def text_fingerprint(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _next_id(module: str) -> int:
    rows = store.rows(module)
    return max((int(r.get("id", 0)) for r in rows), default=0) + 1


def _append(module: str, values: dict[str, Any], *, status: str,
            pending: bool, abnormal: bool = False) -> dict[str, Any]:
    row = {"id": _next_id(module), "status": status, "pending": pending, "abnormal": abnormal}
    row.update(values)
    store.rows(module).append(row)
    return row


class EncodingRecoveryService(BaseModuleService):
    """失败编码记录与恢复流程服务。"""

    MODULE = RECOVERY_TABLE
    ENTITY = "失败编码记录"
    OPERATION_TERM = "恢复"
    LIST_FIELDS = [
        "恢复编号", "关联批次", "附件名", "原始编码", "目标编码",
        "记录类型", "原错误", "恢复编码", "原始字节数",
    ]
    REQUIRED_FIELDS = ["关联批次", "原始编码", "原始字节HEX"]
    STATUS_ORDER = ["待恢复", "待确认", "已恢复", "已替代"]
    ACTION_RULES = {"受理更正": "待确认", "确认恢复": "已恢复", "另发恢复": "待确认"}
    NEGATIVE_ACTIONS = []

    def list_entries(self, *, filters: dict[str, str] | None = None,
                     keyword: str | None = None, status: str | None = None,
                     page: int = 1, size: int = 20
                     ) -> tuple[list[dict[str, Any]], int]:
        """列表不返回原始字节与更正明细，详情接口才展开。"""
        rows = list(store.rows(self.MODULE))
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword and self.LIST_FIELDS:
            key = keyword.casefold()
            rows = [
                row for row in rows
                if any(key in str(row.get(field, "")).casefold()
                       for field in self.LIST_FIELDS)
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        items = [self._list_view(row) for row in rows[start:start + size]]
        return items, total

    def _list_view(self, row: dict[str, Any]) -> dict[str, Any]:
        view = {field: row.get(field, "") for field in self.LIST_FIELDS}
        view.update({
            "id": row.get("id"),
            "status": row.get("status"),
            "pending": row.get("pending"),
            "abnormal": row.get("abnormal"),
            "编码声明": row.get("原始编码"),
            "原字节HEX短": str(row.get("原始字节HEX") or "")[:24],
            "失败编码编号": row.get("失败编码编号"),
            "父恢复编号": row.get("父恢复编号") or "",
            "另发恢复编号": row.get("另发恢复编号") or "",
        })
        return view

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """界面登记走的也是编码归一：未知声明进失败记录，空字节按合法空附件核定。"""
        missing = [
            field for field in ("关联批次", "原始编码")
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        raw = values.get("原始字节HEX", values.get("原始字节", ""))
        raw_bytes = parse_byte_input(raw)
        entry = self.register_failure(
            batch=str(values.get("关联批次") or "").strip(),
            attachment=str(values.get("附件名") or ""),
            declared_encoding=str(values.get("原始编码") or ""),
            target_encoding=str(values.get("目标编码") or "UTF-8"),
            strategy=str(values.get("替换策略") or "REPLACE"),
            raw_bytes=raw_bytes,
            reason=str(values.get("原错误") or "编码声明无法识别，未执行转码"),
            norm_no=str(values.get("原转码编号") or f"EN-{values.get('关联批次')}"),
            operator=str(values.get("登记操作者") or "operator"),
        )
        return entry, []

    # ------------------------------------------------------------------
    # 登记与查询
    # ------------------------------------------------------------------
    def register_failure(self, *, batch: str, attachment: str,
                         declared_encoding: str, target_encoding: str,
                         strategy: str, raw_bytes: bytes, reason: str,
                         norm_no: str, operator: str = "system",
                         template_no: str = "") -> dict[str, Any]:
        """登记失败编码记录；同一转码编号 + 同一字节只保留唯一原记录。"""
        with _RECOVERY_LOCK:
            return self._register_failure_locked(
                batch=batch, attachment=attachment,
                declared_encoding=declared_encoding, target_encoding=target_encoding,
                strategy=strategy, raw_bytes=raw_bytes, reason=reason,
                norm_no=norm_no, operator=operator)

    def _register_failure_locked(self, *, batch: str, attachment: str,
                                 declared_encoding: str, target_encoding: str,
                                 strategy: str, raw_bytes: bytes, reason: str,
                                 norm_no: str, operator: str) -> dict[str, Any]:
        raw_hex = raw_bytes.hex()
        existing = self._find_original(norm_no, raw_hex, batch)
        if existing is not None:
            return dict(existing)

        rows = store.rows(self.MODULE)
        serial = len([
            r for r in rows if str(r.get("关联批次")) == batch and r.get("失败编码编号")
        ]) + 1
        failure_no = f"FE-{batch}-{serial:02d}"
        recovery_no = f"RC-{batch}-{serial:02d}"
        entry = {
            "id": _next_id(self.MODULE),
            "status": "待恢复",
            "pending": True,
            "abnormal": True,
            "恢复编号": recovery_no,
            "失败编码编号": failure_no,
            "关联批次": batch,
            "附件名": attachment,
            "原始编码": declared_encoding,
            "目标编码": target_encoding,
            "替换策略": strategy,
            "原始字节HEX": raw_hex,
            "原始字节数": byte_length(raw_bytes),
            "原错误": reason,
            "原转码编号": norm_no,
            "记录类型": "声明无法识别" if self._unknown_declaration(declared_encoding) else "转码失败",
            "登记时间": "",
            "登记操作者": operator,
            "恢复编码": "",
            "恢复字段数": "0",
            "恢复时间": "",
            "受理回执编号": "",
            "父恢复编号": "",
            "另发原因": "",
            "候选编码": ",".join(c["编码"] for c in self.candidate_decodings(raw_bytes)),
            "更正记录": [],
            "历史声明": declared_encoding,
        }
        rows.append(entry)
        self._write_ledger(
            recovery_no, "转码结论",
            f"声明 {declared_encoding or '空'} 转码失败：{reason}",
            f"原始字节 {len(raw_bytes)} 字节已保留，登记失败编码记录 {failure_no}",
            operator, "encoding_norm")
        return dict(entry)

    def _find_original(self, norm_no: str, raw_hex: str, batch: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if (row.get("原转码编号") == norm_no and row.get("原始字节HEX") == raw_hex
                    and row.get("关联批次") == batch):
                return row
        return None

    def _find(self, recovery_no: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("恢复编号") == recovery_no:
                return row
        return None

    @staticmethod
    def _unknown_declaration(declared: str) -> bool:
        from app.services.encoding_norm import declaration_kind
        return declaration_kind(declared) != "recognized"

    def list_failures(self, status: str | None = None, keyword: str | None = None,
                      page: int = 1, size: int = 50) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(self.MODULE))
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if keyword:
            key = keyword.casefold()
            rows = [
                r for r in rows
                if key in str(r.get("恢复编号") or "").casefold()
                or key in str(r.get("关联批次") or "").casefold()
                or key in str(r.get("原始编码") or "").casefold()
            ]
        total = len(rows)
        start = (page - 1) * size
        items = [self._list_view(row) for row in rows[start:start + size]]
        return items, total

    def failure_summary(self, recovery_no: str) -> dict[str, Any] | None:
        row = self._find(recovery_no)
        if row is None:
            return None
        raw_bytes = bytes.fromhex(str(row.get("原始字节HEX") or ""))
        return {
            "恢复编号": row.get("恢复编号"),
            "失败编码编号": row.get("失败编码编号"),
            "关联批次": row.get("关联批次"),
            "附件名": row.get("附件名"),
            "附件类型": "空附件" if len(raw_bytes) == 0 else "含字节附件",
            "记录类型": row.get("记录类型"),
            "编码声明": row.get("原始编码"),
            "历史声明": row.get("历史声明"),
            "目标编码": row.get("目标编码"),
            "替换策略": row.get("替换策略"),
            "原始字节HEX": row.get("原始字节HEX"),
            "原始字节数": row.get("原始字节数"),
            "原错误": row.get("原错误"),
            "原转码编号": row.get("原转码编号"),
            "status": row.get("status"),
        }

    def detail(self, recovery_no: str) -> dict[str, Any] | None:
        """失败详情：原错误、原字节、候选解码、受理回执、已恢复字段与读入资格。"""
        row = self._find(recovery_no)
        if row is None:
            return None
        raw_bytes = bytes.fromhex(str(row.get("原始字节HEX") or ""))
        candidates = self.candidate_decodings(raw_bytes)
        attempts = [dict(a) for a in row.get("更正记录") or []]
        accepted = next(
            (a for a in attempts if a.get("本次采用") == "是"
             and a.get("状态") in ("已受理", "已恢复")), None)
        fields = self.recovered_fields(recovery_no)
        fork_no = str(row.get("另发恢复编号") or "")
        if not fork_no and accepted is not None:
            fork_no = str(accepted.get("另发恢复编号") or "")
        detail = self.failure_summary(recovery_no) or {}
        detail.update({
            "候选解码": candidates,
            "更正记录": attempts,
            "既有受理": accepted,
            "受理回执": accepted.get("回执") if accepted else None,
            "恢复编码": row.get("恢复编码"),
            "恢复字段数": row.get("恢复字段数"),
            "恢复字段": fields,
            "预测字段": self.preview_fields(row, accepted),
            "原记录唯一": self._original_record_unique(row),
            "另发恢复编号": fork_no,
            "父恢复编号": row.get("父恢复编号") or "",
            "读入资格": self.read_eligibility(row.get("关联批次") or ""),
        })
        return detail

    def preview_fields(self, row: dict[str, Any],
                       accepted: dict[str, Any] | None) -> dict[str, Any] | None:
        """按既有受理的编码试解原字节，展示实际会进入字段解析的取值。"""
        if not accepted:
            return None
        encoding = str(accepted.get("采用编码") or "")
        if not encoding:
            return None
        raw_bytes = bytes.fromhex(str(row.get("原始字节HEX") or ""))
        decodable, text, error = self._decode_with(raw_bytes, encoding)
        if not decodable:
            return {"编码": encoding, "可解码": False, "说明": error, "行": []}
        lines = text.splitlines()
        if not lines:
            return {"编码": encoding, "可解码": True, "说明": "空附件，无字段",
                    "行": [], "字段数": 0}
        header = [c.strip() for c in lines[0].split(",")]
        predicted: list[dict[str, Any]] = []
        for offset, line in enumerate(lines[1:], start=2):
            cells = line.split(",")
            predicted.append({
                "解析行号": str(offset),
                "取值": {name: (cells[pos] if pos < len(cells) else "")
                         for pos, name in enumerate(header) if name},
            })
        return {
            "编码": encoding, "可解码": True,
            "说明": f"按 {encoding} 解码出 {len(lines) - 1} 行数据",
            "字段数": len(predicted) * len([h for h in header if h]),
            "行": predicted,
        }

    def _original_record_unique(self, row: dict[str, Any]) -> bool:
        """同一转码编号、同一字节、同一批次只应有一条原失败记录（另发恢复不算原记录）。"""
        matches = [
            r for r in store.rows(self.MODULE)
            if r.get("原转码编号") == row.get("原转码编号")
            and r.get("原始字节HEX") == row.get("原始字节HEX")
            and r.get("关联批次") == row.get("关联批次")
            and not r.get("父恢复编号")
        ]
        return len(matches) == 1

    # ------------------------------------------------------------------
    # 候选解码与更正受理
    # ------------------------------------------------------------------
    def candidate_decodings(self, raw_bytes: bytes) -> list[dict[str, Any]]:
        """列出可成功解码的候选编码；相同结果指纹的编码不算不同更正。"""
        candidates: list[dict[str, Any]] = []
        for encoding in CANDIDATE_ENCODINGS:
            try:
                text = raw_bytes.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
            first_line = text.splitlines()[0] if text.splitlines() else ""
            candidates.append({
                "编码": encoding,
                "可解码": True,
                "字符数": len(text),
                "结果指纹": text_fingerprint(text),
                "首行": first_line[:80],
            })
        return candidates

    def _decode_with(self, raw_bytes: bytes, encoding: str) -> tuple[bool, str, str]:
        try:
            return True, raw_bytes.decode(encoding), ""
        except UnicodeDecodeError as exc:
            return False, "", f"按 {encoding} 解码失败: {exc}"
        except LookupError as exc:
            return False, "", f"编码「{encoding}」无法识别: {exc}"

    def submit_correction(self, recovery_no: str, encoding: str, source: str,
                          operator: str, occurred_at: str = "") -> dict[str, Any]:
        """受理一次更正，返回明确说明“本次受理了哪一条”的回执。

        渠道更正优先：若既有受理是人工选码，渠道更正会覆盖它并成为新的本次受理；
        反之若来件优先级更低或结果不同，本次不采用，按回执另发恢复单。
        """
        row = self._find(recovery_no)
        if row is None:
            return {"ok": False, "message": f"恢复单 {recovery_no} 不存在", "回执": None}
        if row.get("status") == "已恢复":
            return {"ok": False, "message": (
                f"恢复单 {recovery_no} 已恢复，原记录与已写字段保持不变，不得再受理更正"
            ), "回执": None}

        encoding = str(encoding or "").strip()
        if not encoding:
            return {"ok": False, "message": "更正必须给出编码声明", "回执": None}
        source_label = SOURCE_ALIASES.get(str(source or "").strip(), str(source or "").strip())
        if source_label not in SOURCE_RANK:
            return {"ok": False, "message": (
                f"受理来源「{source}」不在允许范围 " f"{list(SOURCE_RANK)}"
            ), "回执": None}

        raw_bytes = bytes.fromhex(str(row.get("原始字节HEX") or ""))
        decodable, text, error = self._decode_with(raw_bytes, encoding)
        fingerprint = text_fingerprint(text) if decodable else ""
        attempt_no = self._next_attempt_no(recovery_no)
        attempts = [dict(a) for a in row.get("更正记录") or []]
        existing = next(
            (a for a in attempts
             if a.get("本次采用") == "是" and a.get("状态") in ("已受理", "已恢复")),
            None)
        receipt_no = f"{attempt_no}-R"

        if not decodable:
            attempt = self._attempt(
                attempt_no, recovery_no, encoding, source_label, operator,
                fingerprint, error, "未采用", "否",
                f"更正 {encoding} 无法解码原始字节：{error}", "继续核对", "", "", occurred_at)
            attempts.append(attempt)
            row["更正记录"] = attempts
            receipt = self._receipt(
                receipt_no, row, attempt, existing, None, "另发恢复",
                f"本次受理：{source_label} {encoding}（解码失败，未采用）；"
                f"原错误保持不变，可另发恢复或重试")
            attempt["回执编号"] = receipt_no
            attempt["回执"] = receipt
            return {"ok": False, "message": attempt["受理结论"], "回执": receipt,
                    "entry": dict(row)}

        if existing is None:
            attempt = self._attempt(
                attempt_no, recovery_no, encoding, source_label, operator,
                fingerprint, f"按 {encoding} 解码成功，{len(text)} 字符", "已受理", "是",
                f"本次受理：{source_label} {encoding}，等待确认恢复", "继续核对",
                "", "", occurred_at)
            attempts.append(attempt)
            row["更正记录"] = attempts
            row["status"] = "待确认"
            row["pending"] = True
            row["受理回执编号"] = receipt_no
            receipt = self._receipt(
                receipt_no, row, attempt, None, None, "继续核对",
                f"本次受理：{source_label} {encoding}，可按回执继续核对并确认恢复")
            attempt["回执编号"] = receipt_no
            attempt["回执"] = receipt
            return {"ok": True, "message": attempt["受理结论"], "回执": receipt,
                    "entry": dict(row)}

        existing_rank = SOURCE_RANK.get(str(existing.get("受理来源") or ""), 0)
        incoming_rank = SOURCE_RANK[source_label]
        same_result = (
            existing.get("采用编码") == encoding
            and existing.get("解码结果指纹") == fingerprint
        )

        if same_result:
            attempt = self._attempt(
                attempt_no, recovery_no, encoding, source_label, operator,
                fingerprint, f"与既有受理结果相同（{len(text)} 字符）", "已受理", "否",
                f"本次受理：沿用既有受理 {existing.get('采用编码')}，结果一致，继续核对",
                "继续核对", "", existing.get("受理编号") or "", occurred_at)
            attempts.append(attempt)
            row["更正记录"] = attempts
            receipt = self._receipt(
                receipt_no, row, attempt, existing, None, "继续核对",
                "本次受理沿用既有受理，结果一致，按既有回执继续核对")
            attempt["回执编号"] = receipt_no
            attempt["回执"] = receipt
            return {"ok": True, "message": attempt["受理结论"], "回执": receipt,
                    "entry": dict(row)}

        if incoming_rank > existing_rank:
            # 渠道更正覆盖人工选码：被覆盖的一条另发恢复单，原声明不会被改写。
            replaced_no = self._fork_for_superseded(row, existing, operator)
            existing["本次采用"] = "否"
            existing["状态"] = "已替代"
            existing["下一步"] = "另发恢复"
            existing["另发恢复编号"] = replaced_no
            attempt = self._attempt(
                attempt_no, recovery_no, encoding, source_label, operator,
                fingerprint, f"按 {encoding} 解码成功，{len(text)} 字符", "已受理", "是",
                f"本次受理：{source_label} {encoding}（覆盖既有受理 "
                f"{existing.get('受理来源')} {existing.get('采用编码')}）",
                "继续核对", replaced_no, existing.get("受理编号") or "", occurred_at)
            attempts.append(attempt)
            row["更正记录"] = attempts
            row["status"] = "待确认"
            row["待确认编码"] = encoding
            row["受理回执编号"] = receipt_no
            receipt = self._receipt(
                receipt_no, row, attempt, existing, replaced_no, "继续核对",
                f"本次受理：{source_label} {encoding}，覆盖了既有受理；"
                f"被覆盖的 {existing.get('采用编码')} 已另发恢复 {replaced_no}")
            attempt["回执编号"] = receipt_no
            attempt["回执"] = receipt
            return {"ok": True, "message": attempt["受理结论"], "回执": receipt,
                    "entry": dict(row), "另发恢复编号": replaced_no}

        # 来件优先级更低或同级结果不同：本次不采用，回执明确另发恢复。
        fork_no = self._fork_for_incoming(
            row, encoding, fingerprint, source_label, len(text), operator)
        attempt = self._attempt(
            attempt_no, recovery_no, encoding, source_label, operator,
            fingerprint, f"按 {encoding} 解码成功，{len(text)} 字符，与既有受理结果不同",
            "未采用", "否",
            f"本次受理：{source_label} {encoding}（未覆盖既有受理 "
            f"{existing.get('受理来源')} {existing.get('采用编码')}，已另发恢复 {fork_no}）",
            "另发恢复", fork_no, existing.get("受理编号") or "", occurred_at)
        attempts.append(attempt)
        row["更正记录"] = attempts
        row["另发恢复编号"] = fork_no
        receipt = self._receipt(
            receipt_no, row, attempt, existing, fork_no, "另发恢复",
            f"本次受理：{source_label} {encoding}，既有受理保持为 "
            f"{existing.get('采用编码')}；请按回执另发恢复 {fork_no}")
        attempt["回执编号"] = receipt_no
        attempt["回执"] = receipt
        return {"ok": True, "message": attempt["受理结论"], "回执": receipt,
                "entry": dict(row), "另发恢复编号": fork_no}

    def _attempt(self, attempt_no: str, recovery_no: str, encoding: str,
                 source: str, operator: str, fingerprint: str, summary: str,
                 status: str, adopted: str, conclusion: str, next_step: str,
                 fork_no: str, replaces: str, occurred_at: str) -> dict[str, Any]:
        return {
            "受理编号": attempt_no,
            "恢复编号": recovery_no,
            "编码声明": encoding,
            "受理来源": source,
            "采用编码": encoding if adopted == "是" else "",
            "解码结果指纹": fingerprint,
            "解码结果摘要": summary,
            "本次采用": adopted,
            "状态": status,
            "受理结论": conclusion,
            "下一步": next_step,
            "覆盖既有受理编号": replaces,
            "另发恢复编号": fork_no,
            "回执编号": "",
            "操作者": operator,
            "受理时间": occurred_at,
        }

    def _receipt(self, receipt_no: str, row: dict[str, Any], attempt: dict[str, Any],
                 existing: dict[str, Any] | None, fork_no: str | None,
                 next_step: str, conclusion: str) -> dict[str, Any]:
        """受理回执：页面据此判断继续核对还是另发恢复。"""
        return {
            "回执编号": receipt_no,
            "恢复编号": row.get("恢复编号"),
            "失败编码编号": row.get("失败编码编号"),
            "关联批次": row.get("关联批次"),
            "本次受理编号": attempt.get("受理编号"),
            "本次受理来源": attempt.get("受理来源"),
            "本次受理编码": attempt.get("编码声明"),
            "本次是否采用": attempt.get("本次采用"),
            "覆盖既有受理": "是" if attempt.get("覆盖既有受理编号") else "否",
            "既有受理编号": existing.get("受理编号") if existing else "",
            "既有受理编码": existing.get("采用编码") if existing else "",
            "另发恢复编号": fork_no or "",
            "下一步": next_step,
            "受理结论": conclusion,
            "原错误": row.get("原错误"),
            "编码声明": row.get("原始编码"),
            "原始字节HEX": row.get("原始字节HEX"),
            "已恢复": row.get("status") == "已恢复",
        }

    def _next_attempt_no(self, recovery_no: str) -> str:
        row = self._find(recovery_no)
        count = len(row.get("更正记录") or []) if row else 0
        return f"{recovery_no}-A{count + 1}"

    def _fork_for_superseded(self, parent: dict[str, Any], superseded: dict[str, Any],
                             operator: str) -> str:
        """被渠道更正覆盖的人工选码另发一张恢复单，保持其编码与结果可单独核对。"""
        for candidate in store.rows(self.MODULE):
            if (candidate.get("父恢复编号") == parent.get("恢复编号")
                    and candidate.get("恢复编码") == superseded.get("采用编码")
                    and candidate.get("另发原因")
                    == f"人工选码 {superseded.get('采用编码')} 被渠道更正覆盖，另行恢复"):
                return str(candidate.get("恢复编号"))
        fork = self._fork_row(
            parent, str(superseded.get("采用编码") or ""),
            str(superseded.get("受理来源") or "人工选码"), operator,
            f"人工选码 {superseded.get('采用编码')} 被渠道更正覆盖，另行恢复")
        self._seed_fork_attempt(fork, superseded, operator)
        return str(fork["恢复编号"])

    def _fork_for_incoming(self, parent: dict[str, Any], encoding: str,
                           fingerprint: str, source: str, text_len: int,
                           operator: str) -> str:
        """未被采用的来件另发一张恢复单，按到来件自身的编码受理。"""
        for candidate in store.rows(self.MODULE):
            if (candidate.get("父恢复编号") == parent.get("恢复编号")
                    and candidate.get("恢复编码") == encoding
                    and candidate.get("另发原因") == f"与既有受理结果冲突，另行恢复 {encoding}"):
                return str(candidate.get("恢复编号"))
        fork = self._fork_row(
            parent, encoding, source, operator,
            f"与既有受理结果冲突，另行恢复 {encoding}")
        accepted = self._attempt(
            f"{fork['恢复编号']}-A1", fork["恢复编号"], encoding, source, operator,
            fingerprint, f"按 {encoding} 解码成功，{text_len} 字符", "已受理", "是",
            f"另发恢复：本次受理 {source} {encoding}", "继续核对", "", "", "")
        accepted["回执编号"] = f"{fork['恢复编号']}-A1-R"
        accepted["回执"] = self._receipt(
            accepted["回执编号"], fork, accepted, None, None, "继续核对",
            f"另发恢复 {fork['恢复编号']} 已受理 {source} {encoding}")
        fork["更正记录"] = [accepted]
        fork["status"] = "待确认"
        return str(fork["恢复编号"])

    def _seed_fork_attempt(self, fork: dict[str, Any], superseded: dict[str, Any],
                           operator: str) -> None:
        accepted = self._attempt(
            f"{fork['恢复编号']}-A1", fork["恢复编号"],
            str(superseded.get("采用编码") or ""),
            str(superseded.get("受理来源") or "人工选码"), operator,
            str(superseded.get("解码结果指纹") or ""),
            str(superseded.get("解码结果摘要") or "沿原受理结果"), "已受理", "是",
            f"另发恢复：沿用原人工选码 {superseded.get('采用编码')}", "继续核对", "", "", "")
        accepted["回执编号"] = f"{fork['恢复编号']}-A1-R"
        accepted["回执"] = self._receipt(
            accepted["回执编号"], fork, accepted, None, None, "继续核对",
            f"另发恢复 {fork['恢复编号']} 已受理原人工选码 {superseded.get('采用编码')}")
        fork["更正记录"] = [accepted]
        fork["status"] = "待确认"

    def _fork_row(self, parent: dict[str, Any], encoding: str, source: str,
                  operator: str, reason: str) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        seq = 1 + len([
            r for r in rows if r.get("父恢复编号") == parent.get("恢复编号")
        ])
        fork = {
            "id": _next_id(self.MODULE),
            "status": "待确认",
            "pending": True,
            "abnormal": True,
            "恢复编号": f"{parent.get('恢复编号')}-F{seq}",
            "失败编码编号": parent.get("失败编码编号"),
            "关联批次": parent.get("关联批次"),
            "附件名": parent.get("附件名"),
            # 历史声明与原字节始终保留，不因更正而改写
            "原始编码": parent.get("原始编码"),
            "目标编码": parent.get("目标编码"),
            "替换策略": parent.get("替换策略"),
            "原始字节HEX": parent.get("原始字节HEX"),
            "原始字节数": parent.get("原始字节数"),
            "原错误": parent.get("原错误"),
            "原转码编号": parent.get("原转码编号"),
            "记录类型": parent.get("记录类型"),
            "登记时间": "",
            "登记操作者": operator,
            "恢复编码": encoding,
            "恢复字段数": "0",
            "恢复时间": "",
            "受理回执编号": "",
            "父恢复编号": parent.get("恢复编号"),
            "另发原因": reason,
            "候选编码": parent.get("候选编码"),
            "更正记录": [],
            "历史声明": parent.get("历史声明") or parent.get("原始编码"),
        }
        rows.append(fork)
        return fork

    # ------------------------------------------------------------------
    # 确认恢复与字段写入
    # ------------------------------------------------------------------
    def confirm_recovery(self, recovery_no: str, operator: str,
                         receipt_no: str = "", occurred_at: str = "") -> dict[str, Any]:
        """确认恢复：按既有受理所用的编码写入字段，重复确认只写入一次。"""
        with _RECOVERY_LOCK:
            return self._confirm_recovery_locked(recovery_no, operator, receipt_no,
                                                 occurred_at)

    def _confirm_recovery_locked(self, recovery_no: str, operator: str,
                                 receipt_no: str = "",
                                 occurred_at: str = "") -> dict[str, Any]:
        row = self._find(recovery_no)
        if row is None:
            return {"ok": False, "message": f"恢复单 {recovery_no} 不存在", "entry": None}
        if row.get("status") == "已恢复":
            fields = self.recovered_fields(recovery_no)
            return {"ok": True, "message": (
                f"恢复单 {recovery_no} 已恢复，本次未重复写入（已写 {len(fields)} 个字段）"
            ), "entry": dict(row), "field_count": len(fields), "duplicated": False}

        accepted = next(
            (a for a in (row.get("更正记录") or [])
             if a.get("本次采用") == "是" and a.get("状态") == "已受理"), None)
        if accepted is None:
            return {"ok": False, "message": (
                f"恢复单 {recovery_no} 尚无已受理的更正，不能确认恢复；"
                f"请先按回执提交更正"
            ), "entry": dict(row)}
        if receipt_no and accepted.get("回执编号") != receipt_no:
            return {"ok": False, "message": (
                f"恢复单 {recovery_no} 本次受理回执为 {accepted.get('回执编号')}，"
                f"与提交的 {receipt_no} 不一致，请重新打开失败详情核对后再确认"
            ), "entry": dict(row), "expected_receipt": accepted.get("回执编号")}

        encoding = str(accepted.get("采用编码") or "")
        raw_bytes = bytes.fromhex(str(row.get("原始字节HEX") or ""))
        decodable, text, error = self._decode_with(raw_bytes, encoding)
        if not decodable:
            return {"ok": False, "message": (
                f"受理编码 {encoding} 无法解码原字节：{error}"
            ), "entry": dict(row)}

        written = self._write_recovered_fields(row, encoding, text, operator)
        self._certify_transcode(row, encoding, text, operator)

        row["status"] = "已恢复"
        row["pending"] = False
        row["abnormal"] = False
        row["恢复编码"] = encoding
        row["恢复字段数"] = str(len(self.recovered_fields(recovery_no)))
        row["恢复时间"] = occurred_at
        row["恢复操作者"] = operator
        accepted["状态"] = "已恢复"
        row["更正记录"] = list(row.get("更正记录") or [])
        batch = str(row.get("关联批次") or "")
        self._write_ledger(
            recovery_no, "编码恢复",
            f"原始声明 {row.get('原始编码')} 失败：{row.get('原错误')}",
            f"按 {encoding} 恢复，写入 {len(written)} 个字段，原字节与原声明保留",
            operator, "encoding_norm")
        self._write_ledger(
            f"字段读入-{row.get('失败编码编号')}", "字段读入资格",
            f"失败编码记录 {row.get('失败编码编号')} 未恢复，字段解析不得读取",
            f"批次 {batch} 按 {encoding} 恢复后已核定，允许字段解析读取 "
            f"{len(written)} 个字段",
            operator, "field_parse")
        return {"ok": True, "message": (
            f"恢复单 {recovery_no} 已按 {encoding} 恢复，本次写入 {len(written)} 个字段，"
            f"重复提交不会再写；读入资格：{self.read_eligibility(batch)['说明']}"
        ), "entry": dict(row), "field_count": len(written), "duplicated": False}

    def _write_recovered_fields(self, row: dict[str, Any], encoding: str,
                                text: str, operator: str) -> list[dict[str, Any]]:
        batch = str(row.get("关联批次") or "")
        recovery_no = str(row.get("恢复编号") or "")
        lines = text.splitlines()
        if not lines:
            return []
        header = [c.strip() for c in lines[0].split(",")]
        existing = self.recovered_fields(recovery_no)
        known = {(f.get("解析行号"), f.get("字段名称")) for f in existing}
        written: list[dict[str, Any]] = []
        for offset, line in enumerate(lines[1:], start=2):
            cells = line.split(",")
            for pos, field_name in enumerate(header):
                if not field_name:
                    continue
                key = (str(offset), field_name)
                if key in known:
                    continue
                value = cells[pos] if pos < len(cells) else ""
                entry = {
                    "id": _next_id(FIELD_TABLE),
                    "status": "解析成功",
                    "pending": False,
                    "abnormal": False,
                    "解析编号": f"FP-{recovery_no}-{offset}-{pos + 1}",
                    "关联批次": batch,
                    "命中模板": "ET-编码恢复",
                    "字段名称": field_name,
                    "原始取值": value,
                    "解析取值": value,
                    "解析耗时": "1",
                    "解析行号": str(offset),
                    "来源恢复": recovery_no,
                    "所用编码": encoding,
                    "恢复操作者": operator,
                }
                store.rows(FIELD_TABLE).append(entry)
                known.add(key)
                written.append(entry)
        return written

    def _certify_transcode(self, row: dict[str, Any], encoding: str, text: str,
                           operator: str) -> None:
        """把恢复结论同步成一条已核定的转码记录，供字段解析核验读入资格。"""
        norm_no = f"EN-{row.get('恢复编号')}"
        existing = next(
            (r for r in store.rows("encoding_norm") if r.get("转码编号") == norm_no),
            None)
        if existing is not None:
            existing["status"] = "已完成"
            existing["pending"] = False
            existing["abnormal"] = False
            existing["转码核定"] = "已核定"
            return
        store.rows("encoding_norm").append({
            "id": _next_id("encoding_norm"),
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "转码编号": norm_no,
            "关联批次": row.get("关联批次"),
            "原始编码": encoding,
            "目标编码": row.get("目标编码"),
            "不可映射字符数": "0",
            "替换策略": row.get("替换策略"),
            "转码后字节": str(len(text.encode("utf-8"))),
            "转码耗时": "1",
            "损字比例": "0.0000",
            "转码结论": (
                f"由失败编码记录 {row.get('失败编码编号')} 按 {encoding} 恢复为无损转码"
            ),
            "转码核定": "已核定",
            "附件名": row.get("附件名"),
            "恢复编号": row.get("恢复编号"),
            "原编码声明": row.get("原始编码"),
        })

    def recovered_fields(self, recovery_no: str) -> list[dict[str, Any]]:
        return [
            {
                "解析编号": f.get("解析编号"),
                "解析行号": f.get("解析行号"),
                "字段名称": f.get("字段名称"),
                "原始取值": f.get("原始取值"),
                "解析取值": f.get("解析取值"),
                "所用编码": f.get("所用编码"),
            }
            for f in store.rows(FIELD_TABLE)
            if f.get("来源恢复") == recovery_no
        ]

    def read_eligibility(self, batch: str) -> dict[str, Any]:
        from app.services.encoding_norm import EncodingNormService
        ok, message, detail = EncodingNormService().verify_read_eligibility(batch)
        return {"可读入": ok, "说明": message, **detail}

    def retry_transcode(self, recovery_no: str, encoding: str, operator: str,
                        occurred_at: str = "") -> dict[str, Any]:
        """转码失败的批次重试：再跑一次转码受理，失败也不会丢原记录。"""
        row = self._find(recovery_no)
        if row is None:
            return {"ok": False, "message": f"恢复单 {recovery_no} 不存在"}
        if row.get("status") == "已恢复":
            return {"ok": False, "message": f"恢复单 {recovery_no} 已恢复，无需重试"}
        encoding = str(encoding or "").strip() or str(row.get("恢复编码") or "")
        if not encoding:
            encoding = next(
                (str(a.get("采用编码") or "") for a in row.get("更正记录") or []
                 if a.get("本次采用") == "是"), "")
        if not encoding:
            return {"ok": False, "message": (
                f"恢复单 {recovery_no} 没有可重试的编码，请显式给出编码声明"
            )}
        result = self.submit_correction(recovery_no, encoding, "manual", operator,
                                        occurred_at)
        row["重试次数"] = str(int(str(row.get("重试次数") or "0")) + 1)
        if result.get("ok"):
            self._write_ledger(
                recovery_no, "转码重试",
                f"原错误：{row.get('原错误')}",
                f"以 {encoding} 重试转码成功，等待确认恢复",
                operator, "encoding_norm")
            result["message"] = f"重试转码已受理：{result.get('message')}"
        return result

    def _write_ledger(self, object_no: str, operation: str, before: str,
                      after: str, operator: str, source: str) -> None:
        """同步处理台账（投递审计）：同一对象同一操作只留一条结论。"""
        for row in store.rows(LEDGER_TABLE):
            if (row.get("关联对象") == object_no
                    and row.get("操作类型") == operation):
                row["变更后值"] = after
                row["操作时间"] = row.get("操作时间") or ""
                return
        _append(LEDGER_TABLE, {
            "审计编号": f"IA-{object_no}-{operation}",
            "关联对象": object_no,
            "操作类型": operation,
            "变更前值": before,
            "变更后值": after,
            "操作者标识": operator,
            "操作来源": source,
            "操作时间": "",
        }, status="已记录", pending=False)
