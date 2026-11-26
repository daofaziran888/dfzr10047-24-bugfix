"""编码归一领域服务：将异构来源文本统一到目标编码表，产出转码记录数据集。

无法识别的编码声明不会中断登记：原始字节与当时的声明会转成失败编码记录留待恢复，
字段解析只接受已经核定（无损或空附件）的转码输出。
"""
from __future__ import annotations

import codecs
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 这些声明表示“来源没有给出可用的编码”，与真正空附件是两件事。
UNKNOWN_DECLARATION_TOKENS = {
    "unknown",
    "unknown-charset",
    "unknown_charset",
    "unknown-encoding",
    "auto",
    "detect",
    "auto-detect",
    "none",
    "未知",
    "未知编码",
    "无法识别",
}

# 只有带这些核定标记的转码记录才允许字段解析读取。
CERTIFIED_MARKS = ("已核定", "已核定（空附件）")


def declaration_kind(declared: str | None) -> str:
    """判定编码声明属于哪一种：missing（未给）/ unrecognized（无法识别）/ recognized（可用）。"""
    text = str(declared or "").strip()
    if not text:
        return "missing"
    if text.casefold() in UNKNOWN_DECLARATION_TOKENS:
        return "unrecognized"
    try:
        codecs.lookup(text)
    except LookupError:
        return "unrecognized"
    return "recognized"


class EncodingNormService(BaseModuleService):
    MODULE = "encoding_norm"
    ENTITY = "转码记录"
    OPERATION_TERM = "标准化"
    LIST_FIELDS = ["转码编号", "关联批次", "原始编码", "目标编码", "不可映射字符数", "替换策略", "转码后字节", "转码耗时"]
    REQUIRED_FIELDS = ["转码编号", "关联批次", "原始编码"]
    STATUS_ORDER = ["待转码", "转码中", "已完成", "存在损字", "无法转码"]
    ACTION_RULES = {"启动转码": "转码中", "确认完成": "已完成", "登记损字": "存在损字"}
    NEGATIVE_ACTIONS = ["登记损字"]

    REPLACE = "REPLACE"
    IGNORE = "IGNORE"
    STRICT = "STRICT"
    STRATEGIES = (REPLACE, IGNORE, STRICT)

    # 超过该比例即判定为严重损字，字段解析不得直接读取转码结果
    LOSSY_RATIO = 0.05

    def transcode(self, raw_bytes: bytes, source_encoding: str,
                  target_encoding: str, strategy: str) -> tuple[bool, bytes, int, str]:
        """执行真实转码，返回 (是否无损, 转码后字节, 不可映射字符数, 结论)。

        声明本身无法识别时不尝试解码，直接返回失败结论而非抛出错误；
        STRICT 策略下遇到不可映射字符直接失败，不产出半成品文本；
        REPLACE 用占位符替换并计数，IGNORE 直接丢弃并计数。
        """
        if strategy not in self.STRATEGIES:
            return False, b"", 0, f"替换策略「{strategy}」不在允许范围 {list(self.STRATEGIES)}"

        if declaration_kind(source_encoding) != "recognized":
            return False, b"", 0, (
                f"编码声明「{str(source_encoding or '').strip() or '空'}」无法识别，未执行转码"
            )
        if declaration_kind(target_encoding) != "recognized":
            return False, b"", 0, (
                f"目标编码「{str(target_encoding or '').strip() or '空'}」无法识别，未执行转码"
            )

        try:
            text = raw_bytes.decode(source_encoding)
        except (UnicodeDecodeError, LookupError) as exc:
            return False, b"", 0, (
                f"原始编码 {source_encoding} 解码失败: {exc}"
            )

        if strategy == self.STRICT:
            try:
                encoded = text.encode(target_encoding)
                return True, encoded, 0, (
                    f"{source_encoding}→{target_encoding} 严格转码无损，{len(encoded)} 字节"
                )
            except UnicodeEncodeError as exc:
                return False, b"", 0, (
                    f"STRICT 策略下存在不可映射字符，转码中止: {exc}"
                )

        errors = "replace" if strategy == self.REPLACE else "ignore"
        encoded = text.encode(target_encoding, errors=errors)
        roundtrip = encoded.decode(target_encoding, errors="replace")
        unmappable = sum(
            1 for original, converted in zip(text, roundtrip)
            if original != converted
        )
        if unmappable == 0:
            return True, encoded, 0, (
                f"{source_encoding}→{target_encoding} 转码完成，无不可映射字符"
            )
        return False, encoded, unmappable, (
            f"{source_encoding}→{target_encoding} 存在 {unmappable} 个不可映射字符"
            f"（策略 {strategy}）"
        )

    def record_transcode(self, norm_no: str, intake_batch: str, raw_bytes: bytes,
                         source_encoding: str, target_encoding: str,
                         strategy: str, elapsed_ms: int,
                         attachment: str = "", operator: str = "system") -> tuple[bool, str, dict[str, Any] | None]:
        """执行转码并把真实计数写入转码记录，同时登记不可映射字符所在行。

        编码声明无法识别时不会抛错丢失记录：写一条“无法转码”的转码记录，
        并把原始字节、当时的声明和原错误转成失败编码记录，等待恢复处理。
        真正空附件（0 字节）与无法识别的声明分开返回：前者核定为空输入。
        """
        rows = store.rows(self.MODULE)
        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        kind = declaration_kind(source_encoding)
        target_kind = declaration_kind(target_encoding)
        empty = len(raw_bytes) == 0

        if target_kind != "recognized":
            reason = f"目标编码「{str(target_encoding or '').strip() or '空'}」无法识别，未执行转码"
            failure = self._register_failure(
                intake_batch, attachment, source_encoding, target_encoding,
                strategy, raw_bytes, reason, norm_no, operator)
            entry = self._failure_entry(
                entry_id, norm_no, intake_batch, source_encoding, target_encoding,
                strategy, elapsed_ms, reason, failure)
            rows.append(entry)
            return False, (
                f"{reason}；已登记失败编码记录 {failure['失败编码编号']}（恢复编号 {failure['恢复编号']}）"
            ), dict(entry)

        if kind != "recognized" and not (empty and kind == "missing"):
            reason = (
                f"编码声明「{str(source_encoding or '').strip() or '空'}」无法识别，未执行转码"
            )
            failure = self._register_failure(
                intake_batch, attachment, source_encoding, target_encoding,
                strategy, raw_bytes, reason, norm_no, operator)
            entry = self._failure_entry(
                entry_id, norm_no, intake_batch, source_encoding, target_encoding,
                strategy, elapsed_ms, reason, failure)
            rows.append(entry)
            self._sync_transcode_ledger(entry, intake_batch, operator)
            return False, (
                f"{reason}；已登记失败编码记录 {failure['失败编码编号']}（恢复编号 {failure['恢复编号']}）"
            ), dict(entry)

        if empty:
            entry = {
                "id": entry_id,
                "status": "已完成",
                "pending": False,
                "abnormal": False,
                "转码编号": norm_no,
                "关联批次": intake_batch,
                "原始编码": source_encoding,
                "目标编码": target_encoding,
                "不可映射字符数": "0",
                "替换策略": strategy,
                "转码后字节": "0",
                "转码耗时": str(elapsed_ms),
                "损字比例": "0.0000",
                "转码结论": "空附件 0 字节，属合法空输入，无需转码，无字段可解析",
                "转码核定": "已核定（空附件）",
                "附件名": attachment,
            }
            rows.append(entry)
            return True, entry["转码结论"], dict(entry)

        ok, encoded, unmappable, message = self.transcode(
            raw_bytes, source_encoding, target_encoding, strategy)

        if not ok and unmappable == 0:
            failure = self._register_failure(
                intake_batch, attachment, source_encoding, target_encoding,
                strategy, raw_bytes, message, norm_no, operator)
            entry = self._failure_entry(
                entry_id, norm_no, intake_batch, source_encoding, target_encoding,
                strategy, elapsed_ms, message, failure)
            rows.append(entry)
            self._sync_transcode_ledger(entry, intake_batch, operator)
            return False, (
                f"{message}；已登记失败编码记录 {failure['失败编码编号']}，可重试或进入恢复"
            ), dict(entry)

        total_chars = len(raw_bytes.decode(source_encoding, errors="replace")) or 1
        lossy_ratio = unmappable / total_chars
        severe = unmappable > 0 and lossy_ratio > self.LOSSY_RATIO

        entry = {
            "id": entry_id,
            "status": ("已完成" if unmappable == 0 else "存在损字"),
            "pending": unmappable > 0,
            "abnormal": severe,
            "转码编号": norm_no,
            "关联批次": intake_batch,
            "原始编码": source_encoding,
            "目标编码": target_encoding,
            "不可映射字符数": str(unmappable),
            "替换策略": strategy,
            "转码后字节": str(len(encoded)),
            "转码耗时": str(elapsed_ms),
            "损字比例": f"{lossy_ratio:.4f}",
            "转码结论": message,
            "转码核定": "未核定" if severe else "已核定",
            "附件名": attachment,
        }
        rows.append(entry)

        if severe:
            self._register_lossy_lines(intake_batch, raw_bytes, source_encoding,
                                       target_encoding, strategy)
            self._sync_transcode_ledger(entry, intake_batch, operator)
            return False, (
                f"{message}，损字比例 {lossy_ratio:.2%} 超过 {self.LOSSY_RATIO:.0%}，"
                f"字段解析不得直接读取转码结果"
            ), dict(entry)
        self._sync_transcode_ledger(entry, intake_batch, operator)
        return ok, message, dict(entry)

    def _failure_entry(self, entry_id: int, norm_no: str, intake_batch: str,
                       source_encoding: str, target_encoding: str, strategy: str,
                       elapsed_ms: int, reason: str,
                       failure: dict[str, Any]) -> dict[str, Any]:
        """构造“无法转码”的转码记录，未知编码不会被记成无损转码。"""
        return {
            "id": entry_id,
            "status": "无法转码",
            "pending": True,
            "abnormal": True,
            "转码编号": norm_no,
            "关联批次": intake_batch,
            "原始编码": source_encoding,
            "目标编码": target_encoding,
            "不可映射字符数": "0",
            "替换策略": strategy,
            "转码后字节": "0",
            "转码耗时": str(elapsed_ms),
            "损字比例": "0.0000",
            "转码结论": reason,
            "转码核定": "未核定",
            "恢复编号": failure["恢复编号"],
            "失败编码编号": failure["失败编码编号"],
        }

    def _register_failure(self, intake_batch: str, attachment: str,
                          source_encoding: str, target_encoding: str,
                          strategy: str, raw_bytes: bytes, reason: str,
                          norm_no: str, operator: str) -> dict[str, Any]:
        """把失败登记为可恢复的失败编码记录（延迟导入避免模块循环依赖）。"""
        from app.services.encoding_recovery import EncodingRecoveryService

        return EncodingRecoveryService().register_failure(
            batch=intake_batch, attachment=attachment,
            declared_encoding=source_encoding, target_encoding=target_encoding,
            strategy=strategy, raw_bytes=raw_bytes, reason=reason,
            norm_no=norm_no, operator=operator,
        )

    def _sync_transcode_ledger(self, entry: dict[str, Any], intake_batch: str,
                               operator: str) -> None:
        """把转码结论同步到处理台账：同一转码编号只留一条结论。"""
        from app.services.encoding_recovery import EncodingRecoveryService

        object_no = str(entry.get("恢复编号") or entry.get("转码编号") or "")
        if not object_no:
            return
        before = (
            f"转码 {entry.get('转码编号')} 未见核定输出"
        )
        after = (
            f"批次 {intake_batch} 转码结论：{entry.get('status')}，"
            f"核定={entry.get('转码核定')}，不可映射字符数={entry.get('不可映射字符数')}"
        )
        if entry.get("恢复编号"):
            after += f"，失败编码记录 {entry.get('失败编码编号')} 待恢复"
        EncodingRecoveryService()._write_ledger(
            object_no, "转码结论", before, after, operator, "encoding_norm")

    def _register_lossy_lines(self, intake_batch: str, raw_bytes: bytes,
                              source_encoding: str, target_encoding: str,
                              strategy: str) -> list[str]:
        """把含不可映射字符的行登记为隔离行，隔离范围由损字位置决定。"""
        text = raw_bytes.decode(source_encoding, errors="replace")
        encoded = text.encode(target_encoding, errors="replace")
        decoded = encoded.decode(target_encoding, errors="replace")
        created: list[str] = []
        quarantine_rows = store.rows("row_quarantine")
        for index, (original_line, converted_line) in enumerate(
                zip(text.splitlines(), decoded.splitlines()), start=1):
            if original_line == converted_line:
                continue
            quarantine_no = f"RQ-{intake_batch}-enc-{index}"
            if any(r.get("隔离编号") == quarantine_no for r in quarantine_rows):
                continue
            entry_id = max((int(r.get("id", 0)) for r in quarantine_rows), default=0) + 1
            quarantine_rows.append({
                "id": entry_id, "status": "已隔离", "pending": True, "abnormal": True,
                "隔离编号": quarantine_no, "关联批次": intake_batch,
                "原始行号": str(index), "失败字段": "encoding",
                "失败原因": (
                    f"编码归一产生不可映射字符（{source_encoding}→{target_encoding}，"
                    f"策略 {strategy}）"
                ),
                "原始行文本": original_line, "复核结论": "",
                "隔离时间": "",
            })
            created.append(quarantine_no)
        return created

    def lossy_scope(self, norm_no: str) -> dict[str, Any]:
        """输出损字范围，供坏行隔离确定隔离行清单。"""
        intake_batch = ""
        unmappable = 0
        for row in store.rows(self.MODULE):
            if row.get("转码编号") == norm_no:
                intake_batch = str(row.get("关联批次") or "")
                text = str(row.get("不可映射字符数") or "0").strip()
                unmappable = int(text) if text.isdigit() else 0
                break
        isolated = [
            {"隔离编号": r.get("隔离编号"), "原始行号": r.get("原始行号")}
            for r in store.rows("row_quarantine")
            if r.get("关联批次") == intake_batch
            and "enc" in str(r.get("隔离编号") or "")
        ]
        return {
            "转码编号": norm_no,
            "关联批次": intake_batch,
            "不可映射字符数": unmappable,
            "隔离行数": len(isolated),
            "隔离行": isolated,
        }

    def certified_output(self, intake_batch: str) -> dict[str, Any] | None:
        """读取批次最新一条已核定的转码输出，没有则返回 None。"""
        candidates = [
            r for r in store.rows(self.MODULE)
            if r.get("关联批次") == intake_batch
            and r.get("status") == "已完成"
            and r.get("转码核定") in CERTIFIED_MARKS
            and not r.get("abnormal")
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: int(r.get("id", 0)))

    def verify_read_eligibility(self, intake_batch: str) -> tuple[bool, str, dict[str, Any]]:
        """核验字段解析的读入资格：需要已核定的转码输出，且失败编码记录已恢复。

        从未登记过编码记录的批次按历史批次放行，避免把旧链路一刀切断。
        """
        norm_rows = [r for r in store.rows(self.MODULE)
                     if r.get("关联批次") == intake_batch]
        failure_rows = [r for r in store.rows("encoding_recovery")
                        if r.get("关联批次") == intake_batch]
        # 另发的恢复单是同一条失败记录的备选路径，不单独阻断；原记录恢复即可读入。
        original_failures = [r for r in failure_rows if not r.get("父恢复编号")]
        pending_failures = [
            r for r in original_failures
            if r.get("status") in ("待恢复", "待确认")
        ]
        certified = self.certified_output(intake_batch)
        detail = {
            "关联批次": intake_batch,
            "转码记录数": len(norm_rows),
            "失败编码记录数": len(failure_rows),
            "未恢复失败记录": [r.get("失败编码编号") for r in pending_failures],
            "已核定转码编号": certified.get("转码编号") if certified else "",
            "核定标记": certified.get("转码核定") if certified else "",
        }
        if pending_failures:
            return False, (
                f"批次 {intake_batch} 存在未恢复的失败编码记录 "
                f"{[r.get('失败编码编号') for r in pending_failures]}，字段解析不得读取"
            ), detail
        if norm_rows and certified is None:
            return False, (
                f"批次 {intake_batch} 没有已核定的转码输出"
                f"（{len(norm_rows)} 条转码记录均未完成核定），字段解析不得读取"
            ), detail
        if certified is None:
            return True, f"批次 {intake_batch} 未登记转码记录，按历史批次放行", detail
        return True, (
            f"批次 {intake_batch} 转码输出 {certified.get('转码编号')} 已核定"
            f"（{certified.get('转码核定')}），失败编码记录已恢复，可供字段解析读取"
        ), detail

    def blocks_field_parse(self, intake_batch: str) -> tuple[bool, str]:
        """没有已核定转码输出或仍有未恢复失败记录的批次不得进入字段解析。"""
        ok, message, _detail = self.verify_read_eligibility(intake_batch)
        return (not ok, message)
