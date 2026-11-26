"""编码恢复链路测试：未知声明、合法空附件、双更正回执与幂等恢复。"""
from __future__ import annotations

import pytest

from app.pipeline.integration import ParsePipelineOrchestrator
from app.services import SERVICES
from app.services.encoding_norm import (
    EncodingNormService,
    declaration_kind,
)
from app.services.encoding_recovery import (
    EncodingRecoveryService,
    parse_byte_input,
    text_fingerprint,
)
from app.store import store


@pytest.fixture(autouse=False)
def fresh_store():
    store.reload_seed()
    yield store
    store.reload_seed()


def rows_of(module: str, **match) -> list[dict]:
    return [
        r for r in store.rows(module)
        if all(str(r.get(k)) == str(v) for k, v in match.items())
    ]


CN_BYTES = "编号,名称,数量\nA001,苹果,12\nA002,香蕉,7".encode("utf-8")
CN_HEX = CN_BYTES.hex()


class TestDeclarationKinds:
    def test_unknown_charset_is_unrecognized(self):
        assert declaration_kind("unknown-charset") == "unrecognized"
        assert declaration_kind("auto") == "unrecognized"
        assert declaration_kind("nope-encoding") == "unrecognized"

    def test_real_codec_is_recognized(self):
        assert declaration_kind("UTF-8") == "recognized"
        assert declaration_kind("GB18030") == "recognized"

    def test_blank_declaration_is_missing(self):
        assert declaration_kind("") == "missing"
        assert declaration_kind(None) == "missing"


class TestTranscodeNoCrash:
    def test_unknown_declaration_registers_instead_of_raising(self, fresh_store):
        svc = SERVICES["encoding_norm"]
        ok, msg, entry = svc.record_transcode(
            "EN-T-UNK", "FI-T-UNK", CN_BYTES, "unknown-charset", "UTF-8",
            "REPLACE", 5, attachment="cn.csv", operator="ops")
        assert ok is False
        # 不是无损转码，也不会被记成已完成
        assert entry["status"] == "无法转码"
        assert entry["转码核定"] == "未核定"
        assert entry["不可映射字符数"] == "0"
        # 失败编码记录保住了原字节与声明
        failure = rows_of("encoding_recovery", **{"恢复编号": entry["恢复编号"]})[0]
        assert failure["原始编码"] == "unknown-charset"
        assert failure["原始字节HEX"] == CN_HEX
        assert len(bytes.fromhex(failure["原始字节HEX"])) == len(CN_BYTES)
        assert "无法识别" in entry["转码结论"]

    def test_unknown_target_encoding_also_registers(self, fresh_store):
        svc = SERVICES["encoding_norm"]
        ok, msg, entry = svc.record_transcode(
            "EN-T-TGT", "FI-T-TGT", CN_BYTES, "UTF-8", "nope-target",
            "REPLACE", 5, operator="ops")
        assert ok is False
        assert entry["status"] == "无法转码"
        assert entry["恢复编号"]

    def test_empty_attachment_is_certified_separately(self, fresh_store):
        svc = SERVICES["encoding_norm"]
        ok, msg, entry = svc.record_transcode(
            "EN-T-EMP", "FI-T-EMP", b"", "UTF-8", "UTF-8",
            "REPLACE", 0, attachment="empty.csv", operator="ops")
        assert ok is True
        assert entry["status"] == "已完成"
        assert entry["转码核定"] == "已核定（空附件）"
        assert "空附件" in entry["转码结论"]
        # 空附件不产生失败编码记录
        assert rows_of("encoding_recovery", **{"关联批次": "FI-T-EMP"}) == []

    def test_empty_bytes_with_unknown_declaration_still_fails(self, fresh_store):
        svc = SERVICES["encoding_norm"]
        ok, _msg, entry = svc.record_transcode(
            "EN-T-EMP2", "FI-T-EMP2", b"", "unknown-charset", "UTF-8",
            "REPLACE", 0, operator="ops")
        assert ok is False
        assert entry["status"] == "无法转码"


class TestRecoveryCorrections:
    def _failed(self, batch="FI-T-R1", text=CN_BYTES):
        ok, _msg, entry = SERVICES["encoding_norm"].record_transcode(
            f"EN-{batch}", batch, text, "unknown-charset", "UTF-8",
            "REPLACE", 1, attachment="cn.csv", operator="ops")
        assert ok is False
        return entry["恢复编号"]

    def test_candidates_differ_only_when_fingerprints_differ(self, fresh_store):
        svc = EncodingRecoveryService()
        candidates = svc.candidate_decodings(CN_BYTES)
        encodings = {c["编码"] for c in candidates}
        assert "UTF-8" in encodings and "GB18030" in encodings
        fingerprints = {c["结果指纹"] for c in candidates}
        assert len(fingerprints) >= 2

    def test_manual_correction_returns_receipt(self, fresh_store):
        recovery_no = self._failed()
        result = EncodingRecoveryService().submit_correction(
            recovery_no, "GB18030", "manual", "ops", "2026-10-01T09:00:00")
        receipt = result["回执"]
        assert receipt["本次受理编码"] == "GB18030"
        assert receipt["本次是否采用"] == "是"
        assert receipt["下一步"] == "继续核对"
        assert receipt["原错误"]
        assert receipt["原始字节HEX"] == CN_HEX

    def test_channel_correction_supersedes_manual_and_forks_it(self, fresh_store):
        recovery_no = self._failed()
        svc = EncodingRecoveryService()
        svc.submit_correction(recovery_no, "GB18030", "manual", "ops", "t1")
        result = svc.submit_correction(
            recovery_no, "UTF-8", "channel-correction", "gateway", "t2")
        receipt = result["回执"]
        assert receipt["本次是否采用"] == "是"
        assert receipt["覆盖既有受理"] == "是"
        assert receipt["既有受理编码"] == "GB18030"
        assert receipt["另发恢复编号"]
        # 被覆盖的人工选码另发恢复，编码仍然可查
        fork = svc.detail(receipt["另发恢复编号"])
        assert fork["恢复编码"] == "GB18030"
        assert fork["编码声明"] == "unknown-charset"
        assert fork["原始字节HEX"] == CN_HEX

    def test_same_tier_different_result_forks_without_overwriting(self, fresh_store):
        recovery_no = self._failed()
        svc = EncodingRecoveryService()
        svc.submit_correction(recovery_no, "UTF-8", "manual", "ops", "t1")
        result = svc.submit_correction(
            recovery_no, "GB18030", "manual", "ops2", "t2")
        receipt = result["回执"]
        assert receipt["本次是否采用"] == "否"
        assert receipt["下一步"] == "另发恢复"
        assert receipt["另发恢复编号"]
        detail = svc.detail(recovery_no)
        assert detail["既有受理"]["采用编码"] == "UTF-8"
        assert detail["既有受理"]["状态"] == "已受理"

    def test_channel_correction_after_manual_keeps_history_declaration(self, fresh_store):
        recovery_no = self._failed()
        svc = EncodingRecoveryService()
        svc.submit_correction(recovery_no, "GB18030", "manual", "ops", "t1")
        svc.submit_correction(recovery_no, "UTF-8", "channel-correction", "gw", "t2")
        detail = svc.detail(recovery_no)
        # 历史报告保留当时的声明与原字节，后来的更正不改写它
        assert detail["编码声明"] == "unknown-charset"
        assert detail["历史声明"] == "unknown-charset"
        assert detail["原始字节HEX"] == CN_HEX
        assert detail["原错误"]

    def test_undecodable_correction_not_adopted(self, fresh_store):
        recovery_no = self._failed()
        result = EncodingRecoveryService().submit_correction(
            recovery_no, "UTF-16", "manual", "ops", "t1")
        assert result["回执"]["本次是否采用"] == "否"
        assert "解码失败" in result["回执"]["受理结论"]

    def test_confirm_requires_matching_receipt(self, fresh_store):
        recovery_no = self._failed()
        svc = EncodingRecoveryService()
        result = svc.submit_correction(recovery_no, "UTF-8", "manual", "ops", "t1")
        receipt_no = result["回执"]["回执编号"]
        bad = svc.confirm_recovery(recovery_no, "ops", "WRONG", "t2")
        assert bad["ok"] is False
        good = svc.confirm_recovery(recovery_no, "ops", receipt_no, "t3")
        assert good["ok"] is True

    def test_confirm_without_correction_rejected(self, fresh_store):
        recovery_no = self._failed()
        result = EncodingRecoveryService().confirm_recovery(recovery_no, "ops", "", "t")
        assert result["ok"] is False
        assert "尚无已受理的更正" in result["message"]

    def test_recovered_fields_written_once(self, fresh_store):
        recovery_no = self._failed("FI-T-R2",
                                   "编号,名称\nB001,橘子".encode("gb18030"))
        svc = EncodingRecoveryService()
        result = svc.submit_correction(recovery_no, "GB18030", "manual", "ops", "t1")
        first = svc.confirm_recovery(
            recovery_no, "ops", result["回执"]["回执编号"], "t2")
        assert first["ok"] is True
        assert first["field_count"] == 2
        second = svc.confirm_recovery(
            recovery_no, "ops", result["回执"]["回执编号"], "t3")
        assert second["ok"] is True
        assert second["duplicated"] is False
        fields = svc.recovered_fields(recovery_no)
        assert len(fields) == 2
        assert {f["解析取值"] for f in fields} == {"B001", "橘子"}
        # 字段解析表里也没有重复行
        assert len(rows_of("field_parse", **{"来源恢复": recovery_no})) == 2

    def test_recovery_writes_certified_transcode_and_ledger(self, fresh_store):
        recovery_no = self._failed("FI-T-R3",
                                   "编号,名称\nC001,葡萄".encode("gb18030"))
        svc = EncodingRecoveryService()
        result = svc.submit_correction(recovery_no, "GB18030", "manual", "ops", "t1")
        svc.confirm_recovery(recovery_no, "ops", result["回执"]["回执编号"], "t2")
        norm_rows = rows_of("encoding_norm", **{"转码编号": f"EN-{recovery_no}"})
        assert norm_rows[0]["转码核定"] == "已核定"
        assert norm_rows[0]["status"] == "已完成"
        ledger = rows_of("intake_audit", **{"关联对象": recovery_no,
                                            "操作类型": "编码恢复"})
        assert ledger
        assert "GB18030" in ledger[0]["变更后值"]

    def test_failure_registration_is_idempotent(self, fresh_store):
        svc = EncodingRecoveryService()
        first = svc.register_failure(
            batch="FI-T-R4", attachment="a.csv", declared_encoding="unknown-charset",
            target_encoding="UTF-8", strategy="REPLACE", raw_bytes=CN_BYTES,
            reason="无法识别", norm_no="EN-FI-T-R4", operator="ops")
        second = svc.register_failure(
            batch="FI-T-R4", attachment="a.csv", declared_encoding="unknown-charset",
            target_encoding="UTF-8", strategy="REPLACE", raw_bytes=CN_BYTES,
            reason="无法识别", norm_no="EN-FI-T-R4", operator="ops")
        assert first["恢复编号"] == second["恢复编号"]
        assert len(rows_of("encoding_recovery", **{"关联批次": "FI-T-R4"})) == 1


class TestEligibilityAndFieldParse:
    def test_unknown_failure_blocks_field_parse(self, fresh_store):
        svc = SERVICES["field_parse"]
        ok, msg, entry = svc.record_parsed_field(
            "FP-T-1", "FI-20260903001", "ET-001", "编号", "A001", "A001",
            1, 2, True)
        assert ok is False
        assert entry is None
        assert "未恢复" in msg

    def test_recovered_failure_allows_field_parse(self, fresh_store):
        svc = SERVICES["field_parse"]
        rec = SERVICES["encoding_recovery"]
        # seed 中已恢复的那一条
        eligible, reason, _detail = svc.check_read_eligibility("FI-20260903002")
        assert eligible is True
        assert "已核定" in reason

    def test_empty_attachment_batch_is_eligible_but_has_no_fields(self, fresh_store):
        svc = SERVICES["encoding_norm"]
        eligible, reason, detail = svc.verify_read_eligibility("FI-20260903003")
        assert eligible is True
        assert detail["核定标记"] == "已核定（空附件）"

    def test_seed_unknown_batch_not_accepted_as_lossless(self, fresh_store):
        norm = rows_of("encoding_norm", **{"转码编号": "EN-005"})[0]
        assert norm["status"] == "无法转码"
        assert norm["转码核定"] == "未核定"
        certified = EncodingNormService().certified_output("FI-20260903001")
        assert certified is None

    def test_lossy_batch_without_certification_blocks(self, fresh_store):
        eligible, _reason, detail = SERVICES["encoding_norm"].verify_read_eligibility(
            "FI-20260902002")
        assert eligible is False
        assert detail["已核定转码编号"] == ""

    def test_unrecorded_legacy_batch_passes(self, fresh_store):
        eligible, reason, _detail = SERVICES["encoding_norm"].verify_read_eligibility(
            "FI-20260901001")
        assert eligible is True


class TestRecoveryApiPayloads:
    def test_hex_input_roundtrip(self):
        assert parse_byte_input(CN_HEX) == CN_BYTES
        assert parse_byte_input("0x" + CN_HEX) == CN_BYTES

    def test_empty_input_is_zero_bytes(self):
        assert parse_byte_input("") == b""

    def test_text_input_falls_back_to_utf8(self):
        assert parse_byte_input("编号,名称") == "编号,名称".encode("utf-8")

    def test_text_fingerprint_distinguishes_results(self):
        assert text_fingerprint("甲") != text_fingerprint("乙")

    def test_retry_transcode_does_not_lose_original(self, fresh_store):
        ok, _msg, entry = SERVICES["encoding_norm"].record_transcode(
            "EN-T-RETRY", "FI-T-RETRY", CN_BYTES, "unknown-charset", "UTF-8",
            "REPLACE", 1, operator="ops")
        recovery_no = entry["恢复编号"]
        result = SERVICES["encoding_recovery"].retry_transcode(
            recovery_no, "UTF-8", "ops", "t")
        assert result["ok"] is True
        detail = SERVICES["encoding_recovery"].detail(recovery_no)
        assert detail["编码声明"] == "unknown-charset"
        assert detail["原始字节HEX"] == CN_HEX
        assert detail["原错误"]


class TestPipelineStillWorks:
    def test_link2_lossy_still_quarantines(self, fresh_store):
        orch = ParsePipelineOrchestrator()
        rows = [
            {"rec_id": "R-1", "rec_timestamp": "2026-09-01 08:10:00",
             "data_value": "100.5", "payload_bytes": "1024"},
            {"rec_id": "R-2", "rec_timestamp": "2026-09-01 08:11:00",
             "data_value": "###", "payload_bytes": "2048"},
        ]
        result = orch.link2_encoding_quarantine(
            batch_id="FI-ENC-1", source_encoding="GB18030", target_encoding="UTF-8",
            unmappable_count=3, replace_strategy="REPLACE", raw_lines=rows,
            template_id="ET-001",
            field_defs=[
                {"name": "rec_id", "type": "str", "nullable": False},
                {"name": "rec_timestamp", "type": "datetime", "nullable": False},
                {"name": "data_value", "type": "float", "nullable": False},
                {"name": "payload_bytes", "type": "int", "nullable": False},
            ],
            norm_time="2026-09-03T10:00:00")
        assert result["lossy"] is True
        assert result["quarantined_rows"] == [2]


class TestLedgerSync:
    def test_transcode_conclusion_written_to_ledger(self, fresh_store):
        svc = SERVICES["encoding_norm"]
        _ok, _msg, entry = svc.record_transcode(
            "EN-LED-1", "FI-LED-1", CN_BYTES, "unknown-charset", "UTF-8",
            "REPLACE", 1, operator="ops")
        ledger = rows_of("intake_audit", **{"关联对象": entry["恢复编号"],
                                            "操作类型": "转码结论"})
        assert ledger
        assert "无法转码" in ledger[0]["变更后值"]

    def test_lossless_conclusion_written_to_ledger(self, fresh_store):
        _ok, _msg, entry = SERVICES["encoding_norm"].record_transcode(
            "EN-LED-2", "FI-LED-2", "R-1,100".encode("utf-8"), "UTF-8", "UTF-8",
            "REPLACE", 1, operator="ops")
        ledger = rows_of("intake_audit", **{"关联对象": "EN-LED-2"})
        assert any("已核定" in r["变更后值"] for r in ledger)

    def test_ledger_not_duplicated(self, fresh_store):
        svc = SERVICES["encoding_norm"]
        svc.record_transcode("EN-LED-3", "FI-LED-3", CN_BYTES, "unknown-charset",
                             "UTF-8", "REPLACE", 1, operator="ops")
        svc.record_transcode("EN-LED-3", "FI-LED-3", CN_BYTES, "unknown-charset",
                             "UTF-8", "REPLACE", 1, operator="ops")
        ledger = rows_of("intake_audit", **{"操作类型": "转码结论"})
        # 两次登记同一条失败记录，台账里只留一条结论
        assert len([r for r in ledger if "FI-LED-3" in r["变更后值"]]) == 1
