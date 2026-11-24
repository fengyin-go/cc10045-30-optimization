"""事件补录稳定性测试：覆盖部分接收、两种估算口径、幂等确认与正式分账规则。

使用独立临时 SQLite 库与临时拒收目录，不污染开发库：
    python -m unittest app.tests.test_event_backfill -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

# 必须在导入 app.* 之前指定独立的库与拒收目录
_TMP = tempfile.mkdtemp(prefix="backfill_test_")
os.environ["CC10045_DB_DIR"] = _TMP
os.environ["CC10045_REJECT_DIR"] = str(Path(_TMP) / "rejects")

from app.database import db  # noqa: E402
from app.services.event_backfill import (  # noqa: E402
    BATCH_FULL, BATCH_PARTIAL, BATCH_PENDING, LINE_ACCEPTED, LINE_PENDING,
    LINE_REJECTED, POLICY_HOLD, POLICY_PARTIAL, backfill_service,
)

SHIFT = "shift-20261124-night"
WINDOW_START = "2026-11-24T00:00:00"
WINDOW_END = "2026-11-24T23:59:59"


def make_csv(rows: list[dict[str, str]]) -> str:
    header = "来源编号,业务时间,归属,归属确认,覆盖对象,事件名称\n"
    lines = [
        f"{r['来源编号']},{r['业务时间']},{r['归属']},{r['归属确认']},{r['覆盖对象']},{r['事件名称']}"
        for r in rows
    ]
    return header + "\n".join(lines) + "\n"


def nine_confirmed_rows() -> list[dict[str, str]]:
    rows = []
    for i in range(1, 10):
        rows.append({
            "来源编号": f"EXT-{i:03d}",
            "业务时间": f"2026-11-24 0{i}:00:00" if i < 10 else "2026-11-24 10:00:00",
            "归属": "北区",
            "归属确认": "是",
            "覆盖对象": f"P{i:03d}",
            "事件名称": "支援登记",
        })
    return rows


def tenth_pending_row() -> dict[str, str]:
    # 有候选归属「南区」但归属尚未确认：属于待核行，后续补齐确认后才计入
    return {
        "来源编号": "EXT-010",
        "业务时间": "2026-11-24 10:30:00",
        "归属": "南区",
        "归属确认": "否",
        "覆盖对象": "P010",
        "事件名称": "支援登记",
    }


class EventBackfillTestCase(unittest.TestCase):
    """九条已核 + 第十条待核的完整业务场景。"""

    def setUp(self) -> None:
        # 每个用例清空补录相关表，保证相互独立
        for table in ("region_ledgers", "coverage_estimates", "backfill_events",
                      "backfill_lines", "backfill_batches"):
            db.execute(f"DELETE FROM {table}")
        # 已确认旧估算：30 人
        backfill_service.register_baseline(
            shift_code=SHIFT, baseline_count=30,
            window_start=WINDOW_START, window_end=WINDOW_END,
        )

    # ----------------------------------------------------------------
    # 场景 1：部分接收 —— 九条确认、第十条待核，市级估算 39 人
    # ----------------------------------------------------------------
    def test_partial_acceptance_publishes_39(self) -> None:
        content = make_csv(nine_confirmed_rows() + [tenth_pending_row()])
        batch = backfill_service.import_backfill_file(
            content, source_org="外部救援机构A", source_file="backfill_10.csv",
            shift_code=SHIFT, batch_no="BF-PARTIAL-01",
        )

        self.assertEqual(batch["total_lines"], 10)
        self.assertEqual(batch["accepted_lines"], 9)
        self.assertEqual(batch["rejected_lines"], 0)
        self.assertEqual(batch["status"], BATCH_PARTIAL)

        statuses = {line["file_line_no"]: line["line_status"] for line in batch["lines"]}
        self.assertEqual(statuses[10], LINE_PENDING)
        self.assertTrue(all(statuses[i] == LINE_ACCEPTED for i in range(1, 10)))

        # 拒收行文件实际落盘，且包含第十条与待核原因
        reject_text = backfill_service.get_reject_file_content(batch["id"])
        self.assertIn("EXT-010", reject_text)
        self.assertIn("归属地区待核", reject_text)

        estimate = backfill_service.publish_city_estimate(
            shift_code=SHIFT, window_start=WINDOW_START, window_end=WINDOW_END,
            policy=POLICY_PARTIAL,
        )
        self.assertEqual(estimate["coverage_count"], 39)
        self.assertEqual(estimate["baseline_count"], 30)
        self.assertEqual(estimate["added_count"], 9)

        # 导入后查询本班聚合发布的估算，拿到的就是 39
        latest = backfill_service.latest_estimate(SHIFT)
        self.assertEqual(latest["coverage_count"], 39)

    def test_estimate_traceable_to_nine_file_lines(self) -> None:
        content = make_csv(nine_confirmed_rows() + [tenth_pending_row()])
        batch = backfill_service.import_backfill_file(
            content, source_org="外部救援机构A", source_file="backfill_10.csv",
            shift_code=SHIFT, batch_no="BF-TRACE-01",
        )
        estimate = backfill_service.publish_city_estimate(
            shift_code=SHIFT, window_start=WINDOW_START, window_end=WINDOW_END,
            policy=POLICY_PARTIAL,
        )

        # 沿估算关联的批次能核对到文件的九行，第十条待核不在范围内
        scope = estimate["scope"]
        basis_lines = scope["basis_lines"]
        self.assertEqual(len(basis_lines), 9)
        self.assertEqual(scope["basis_batches"], [batch["id"]])
        line_nos = sorted(item["file_line_no"] for item in basis_lines)
        self.assertEqual(line_nos, list(range(1, 10)))
        self.assertTrue(all(item["batch_id"] == batch["id"] for item in basis_lines))

    # ----------------------------------------------------------------
    # 场景 2：整批待核 —— 暂保旧估算口径仍显示 30 人
    # ----------------------------------------------------------------
    def test_full_pending_hold_baseline_keeps_30(self) -> None:
        pending_rows = []
        for i in range(1, 11):
            pending_rows.append({
                "来源编号": f"NEW-{i:03d}",
                "业务时间": f"2026-11-24 1{i % 10}:00:00",
                "归属": "东区",
                "归属确认": "否",
                "覆盖对象": f"N{i:03d}",
                "事件名称": "支援登记",
            })
        batch = backfill_service.import_backfill_file(
            make_csv(pending_rows), source_org="外部救援机构B",
            source_file="pending_10.csv", shift_code=SHIFT, batch_no="BF-HOLD-01",
        )
        self.assertEqual(batch["status"], BATCH_PENDING)
        self.assertEqual(batch["accepted_lines"], 0)

        estimate = backfill_service.publish_city_estimate(
            shift_code=SHIFT, window_start=WINDOW_START, window_end=WINDOW_END,
            policy=POLICY_HOLD,
        )
        self.assertEqual(estimate["coverage_count"], 30)
        self.assertEqual(estimate["added_count"], 0)
        self.assertEqual(estimate["scope"]["basis_lines"], [])
        self.assertEqual(backfill_service.latest_estimate(SHIFT)["coverage_count"], 30)

    def test_partial_policy_ignores_pending_lines_too(self) -> None:
        # 即使选用 partial_accepted，待核行也绝不可能被计入
        content = make_csv(nine_confirmed_rows() + [tenth_pending_row()])
        backfill_service.import_backfill_file(
            content, source_org="外部救援机构A", source_file="backfill_10.csv",
            shift_code=SHIFT, batch_no="BF-PARTIAL-02",
        )
        coverage = backfill_service.window_coverage(WINDOW_START, WINDOW_END)
        self.assertEqual(coverage["coverage_count"], 9)
        self.assertTrue(all(line["source_event_no"] != "EXT-010" for line in coverage["basis_lines"]))

    # ----------------------------------------------------------------
    # 场景 3：补齐第十行后正式核对 40 人
    # ----------------------------------------------------------------
    def test_confirm_tenth_line_then_40(self) -> None:
        content = make_csv(nine_confirmed_rows() + [tenth_pending_row()])
        batch = backfill_service.import_backfill_file(
            content, source_org="外部救援机构A", source_file="backfill_10.csv",
            shift_code=SHIFT, batch_no="BF-FULL-01",
        )
        result = backfill_service.confirm_line(batch["id"], 10, region="南区")
        self.assertTrue(result["ok"])

        refreshed = backfill_service.get_batch(batch["id"])
        self.assertEqual(refreshed["status"], BATCH_FULL)
        self.assertEqual(refreshed["accepted_lines"], 10)
        # 全部行处理完后，拒收行文件应被清除，不再残留已确认的第十行
        self.assertEqual(refreshed["rejected_file_path"], "")
        self.assertEqual(backfill_service.get_reject_file_content(batch["id"]), "")

        estimate = backfill_service.publish_city_estimate(
            shift_code=SHIFT, window_start=WINDOW_START, window_end=WINDOW_END,
            policy=POLICY_PARTIAL,
        )
        self.assertEqual(estimate["coverage_count"], 40)
        line_nos = sorted(item["file_line_no"] for item in estimate["scope"]["basis_lines"])
        self.assertEqual(line_nos, list(range(1, 11)))

        # 正式地区分账：只含归属已确认的事件，按地区去重并留存行号
        ledger_result = backfill_service.publish_regional_ledgers(
            shift_code=SHIFT, window_start=WINDOW_START, window_end=WINDOW_END,
            estimate_id=estimate["id"],
        )
        ledgers = ledger_result["ledgers"]
        regions = {item["region"]: item for item in ledgers}
        self.assertEqual(regions["北区"]["coverage_count"], 9)
        self.assertEqual(regions["南区"]["coverage_count"], 1)
        south_refs = json.loads(regions["南区"]["basis_line_refs"])
        self.assertEqual(south_refs[0]["file_line_no"], 10)

    # ----------------------------------------------------------------
    # 场景 4：重复传同一补录文件不会变成 50
    # ----------------------------------------------------------------
    def test_resubmit_same_file_does_not_double_count(self) -> None:
        content = make_csv(nine_confirmed_rows() + [tenth_pending_row()])
        first = backfill_service.import_backfill_file(
            content, source_org="外部救援机构A", source_file="backfill_10.csv",
            shift_code=SHIFT, batch_no="BF-DUP-01",
        )
        backfill_service.confirm_line(first["id"], 10, region="南区")
        self.assertEqual(
            backfill_service.publish_city_estimate(
                shift_code=SHIFT, window_start=WINDOW_START, window_end=WINDOW_END,
                policy=POLICY_PARTIAL)["coverage_count"],
            40,
        )

        second = backfill_service.import_backfill_file(
            content, source_org="外部救援机构A", source_file="backfill_10.csv",
            shift_code=SHIFT, batch_no="BF-DUP-02",
        )
        # 第二批十行全部因重复被拒收，实际接收事件不增加
        self.assertEqual(second["accepted_lines"], 0)
        self.assertEqual(second["rejected_lines"], 10)
        self.assertTrue(all(line["line_status"] == LINE_REJECTED for line in second["lines"]))

        total_events = db.count("backfill_events")
        self.assertEqual(total_events, 10)

        estimate = backfill_service.publish_city_estimate(
            shift_code=SHIFT, window_start=WINDOW_START, window_end=WINDOW_END,
            policy=POLICY_PARTIAL,
        )
        self.assertEqual(estimate["coverage_count"], 40)
        self.assertNotEqual(estimate["coverage_count"], 50)

    # ----------------------------------------------------------------
    # 场景 5：补交拒收行时保留原文件拒收原因
    # ----------------------------------------------------------------
    def test_resubmit_keeps_original_reject_reason(self) -> None:
        bad_row = {
            "来源编号": "EXT-BAD",
            "业务时间": "not-a-time",
            "归属": "北区",
            "归属确认": "是",
            "覆盖对象": "PBAD",
            "事件名称": "支援登记",
        }
        first = backfill_service.import_backfill_file(
            make_csv([bad_row]), source_org="外部救援机构C",
            source_file="bad.csv", shift_code=SHIFT, batch_no="BF-REJ-01",
        )
        original_line = first["lines"][0]
        self.assertEqual(original_line["line_status"], LINE_REJECTED)
        self.assertIn("业务时间", original_line["reject_reason"])

        # 原样补交同一条事件（业务时间仍是坏的）
        second = backfill_service.import_backfill_file(
            make_csv([bad_row]), source_org="外部救援机构C",
            source_file="bad_resubmit.csv", shift_code=SHIFT, batch_no="BF-REJ-02",
        )
        new_line = second["lines"][0]
        self.assertEqual(new_line["line_status"], LINE_REJECTED)
        self.assertIn("沿用原拒收原因", new_line["reject_reason"])
        self.assertIn("BF-REJ-01", new_line["reject_reason"])
        self.assertIn("业务时间", new_line["reject_reason"])
        self.assertEqual(db.count("backfill_events"), 0)

        # 补交修好的同一条事件：来源编号相同，确认后只计一次
        fixed_row = {**bad_row, "业务时间": "2026-11-24 11:00:00"}
        fixed = backfill_service.import_backfill_file(
            make_csv([fixed_row]), source_org="外部救援机构C",
            source_file="bad_fixed.csv", shift_code=SHIFT, batch_no="BF-REJ-03",
        )
        self.assertEqual(fixed["accepted_lines"], 1)
        self.assertEqual(db.count("backfill_events"), 1)

    # ----------------------------------------------------------------
    # 场景 6：坏数据不当零、未知归属不进正式分账
    # ----------------------------------------------------------------
    def test_bad_data_never_counted_as_zero(self) -> None:
        rows = nine_confirmed_rows() + [
            {  # 缺来源编号 + 坏时间
                "来源编号": "",
                "业务时间": "n/a",
                "归属": "北区",
                "归属确认": "是",
                "覆盖对象": "PBAD1",
                "事件名称": "支援登记",
            },
            {  # 缺覆盖对象
                "来源编号": "EXT-X2",
                "业务时间": "2026-11-24 12:00:00",
                "归属": "北区",
                "归属确认": "是",
                "覆盖对象": "",
                "事件名称": "支援登记",
            },
        ]
        batch = backfill_service.import_backfill_file(
            make_csv(rows), source_org="外部救援机构D",
            source_file="mixed.csv", shift_code=SHIFT, batch_no="BF-BAD-01",
        )
        self.assertEqual(batch["accepted_lines"], 9)
        self.assertEqual(batch["rejected_lines"], 2)
        bad_lines = [line for line in batch["lines"] if line["line_status"] == LINE_REJECTED]
        self.assertTrue(all(line["reject_reason"] for line in bad_lines))

        # 窗口聚合只认真实接收事件，坏行既不出现也不被当成 0 人
        coverage = backfill_service.window_coverage(WINDOW_START, WINDOW_END)
        self.assertEqual(coverage["coverage_count"], 9)
        self.assertEqual(coverage["event_count"], 9)

        estimate = backfill_service.publish_city_estimate(
            shift_code=SHIFT, window_start=WINDOW_START, window_end=WINDOW_END,
            policy=POLICY_PARTIAL,
        )
        self.assertEqual(estimate["coverage_count"], 39)

        ledger_result = backfill_service.publish_regional_ledgers(
            shift_code=SHIFT, window_start=WINDOW_START, window_end=WINDOW_END)
        # 正式分账只有北区 9 人；坏行没有产生任何地区分账条目
        self.assertEqual(len(ledger_result["ledgers"]), 1)
        self.assertEqual(ledger_result["ledgers"][0]["coverage_count"], 9)

    def test_unconfirmed_region_never_enters_ledger(self) -> None:
        content = make_csv(nine_confirmed_rows() + [tenth_pending_row()])
        backfill_service.import_backfill_file(
            content, source_org="外部救援机构A", source_file="backfill_10.csv",
            shift_code=SHIFT, batch_no="BF-LEDGER-01",
        )
        ledger_result = backfill_service.publish_regional_ledgers(
            shift_code=SHIFT, window_start=WINDOW_START, window_end=WINDOW_END)
        regions = {item["region"] for item in ledger_result["ledgers"]}
        self.assertNotIn("", regions)
        self.assertEqual(regions, {"北区"})

    # ----------------------------------------------------------------
    # 场景 7：估算范围可回查
    # ----------------------------------------------------------------
    def test_estimate_scope_can_be_replayed(self) -> None:
        content = make_csv(nine_confirmed_rows() + [tenth_pending_row()])
        batch = backfill_service.import_backfill_file(
            content, source_org="外部救援机构A", source_file="backfill_10.csv",
            shift_code=SHIFT, batch_no="BF-SCOPE-01",
        )
        estimate = backfill_service.publish_city_estimate(
            shift_code=SHIFT, window_start=WINDOW_START, window_end=WINDOW_END,
            policy=POLICY_PARTIAL,
        )
        # 用估算冻结的窗口范围重放聚合，必须得到与发布时一致的新增人数
        scope = estimate["scope"]
        replay = backfill_service.window_coverage(scope["window_start"], scope["window_end"])
        self.assertEqual(replay["coverage_count"], estimate["added_count"])
        self.assertEqual(
            sorted((l["batch_id"], l["file_line_no"]) for l in replay["basis_lines"]),
            sorted((l["batch_id"], l["file_line_no"]) for l in scope["basis_lines"]),
        )
        # 通过批次 id 能反查到具体批次与行
        traced = backfill_service.get_batch(batch["id"])
        self.assertEqual(len(traced["accepted_events"]), 9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
