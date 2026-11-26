"""字段解析领域服务：按命中模板逐字段抽取取值并记录解析质量，产出解析字段数据集。

读入资格由编码归一核验：必须有已核定的转码输出，且该批次的失败编码记录已恢复；
未知编码不会流入字段解析。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class FieldParseService(BaseModuleService):
    MODULE = "field_parse"
    ENTITY = "解析字段"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["解析编号", "关联批次", "命中模板", "字段名称", "原始取值", "解析取值", "解析耗时", "解析行号"]
    REQUIRED_FIELDS = ["解析编号", "关联批次", "命中模板"]
    STATUS_ORDER = ["待解析", "解析中", "解析成功", "部分失败"]
    ACTION_RULES = {"启动解析": "解析中", "确认成功": "解析成功", "登记部分失败": "部分失败"}
    NEGATIVE_ACTIONS = ["登记部分失败"]

    def check_read_eligibility(self, intake_batch: str) -> tuple[bool, str, dict[str, Any]]:
        """凭有效转码输出与失败编码记录核验读入资格。"""
        from app.services.encoding_norm import EncodingNormService
        return EncodingNormService().verify_read_eligibility(intake_batch)

    def record_parsed_field(self, parse_no: str, intake_batch: str, template_no: str,
                            field_name: str, raw_value: str, parsed_value: str,
                            elapsed_ms: int, row_num: int,
                            success: bool,
                            skip_eligibility: bool = False
                            ) -> tuple[bool, str, dict[str, Any] | None]:
        """登记单字段解析结果；读入资格不满足时拒绝落库，未知编码不流入字段解析。"""
        if not skip_eligibility:
            eligible, reason, _detail = self.check_read_eligibility(intake_batch)
            if not eligible:
                return False, reason, None
        rows = store.rows(self.MODULE)
        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": entry_id,
            "status": "解析成功" if success else "部分失败",
            "pending": not success,
            "abnormal": not success,
            "解析编号": parse_no,
            "关联批次": intake_batch,
            "命中模板": template_no,
            "字段名称": field_name,
            "原始取值": raw_value,
            "解析取值": parsed_value,
            "解析耗时": str(elapsed_ms),
            "解析行号": str(row_num),
        }
        rows.append(entry)
        return True, f"字段 {field_name}（行 {row_num}）已登记", entry

    def parse_with_record(self, parse_no: str, intake_batch: str, template_no: str,
                          field_name: str, raw_value: str, parsed_value: str,
                          elapsed_ms: int, row_num: int, success: bool,
                          ) -> tuple[bool, str, dict[str, Any] | None]:
        """带读入资格核验的解析登记入口，页面与管线都走这一条。"""
        return self.record_parsed_field(
            parse_no, intake_batch, template_no, field_name, raw_value,
            parsed_value, elapsed_ms, row_num, success)

    def failed_fields_of_batch(self, intake_batch: str) -> list[dict[str, Any]]:
        """输出解析失败字段清单（含行号），坏行隔离据此建隔离行。"""
        return [
            {
                "解析编号": r.get("解析编号"),
                "字段名称": r.get("字段名称"),
                "原始取值": r.get("原始取值"),
                "解析行号": r.get("解析行号"),
            }
            for r in store.rows(self.MODULE)
            if r.get("关联批次") == intake_batch and r.get("status") == "部分失败"
        ]

    def parsed_rows_of_batch(self, intake_batch: str) -> dict[str, dict[str, Any]]:
        """按行号聚合解析成功的取值，供抽样复核与输出契约取数。"""
        grouped: dict[str, dict[str, Any]] = {}
        for r in store.rows(self.MODULE):
            if r.get("关联批次") != intake_batch:
                continue
            line = str(r.get("解析行号") or "")
            if not line:
                continue
            bucket = grouped.setdefault(line, {})
            bucket[str(r.get("字段名称"))] = r.get("解析取值")
            bucket["__status"] = (
                "部分失败" if r.get("status") == "部分失败"
                else bucket.get("__status", "解析成功")
            )
        return grouped

    def success_rate(self, intake_batch: str) -> float:
        """批次字段级解析成功率，供解析指标交叉核对。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("关联批次") == intake_batch]
        if not rows:
            return 0.0
        return sum(1 for r in rows if r.get("status") == "解析成功") / len(rows)

    def template_hit_stats(self) -> dict[str, int]:
        """按命中模板统计解析字段数，供模板版本过渡期命中分布核对。"""
        stats: dict[str, int] = {}
        for r in store.rows(self.MODULE):
            key = str(r.get("命中模板") or "")
            if key:
                stats[key] = stats.get(key, 0) + 1
        return stats
