import unittest

from scripts.scan_summary import (
    FIXED_HEADERS,
    SummaryUpdateError,
    aggregate_current_week,
    plan_summary_update,
)


class ScanSummaryAggregationTests(unittest.TestCase):
    def test_slow_service_aggregates_duplicate_keys(self):
        rows = [
            {
                "时间窗口结束": "2026-08-24 01:00:00",
                "应用英文名": "app-a",
                "服务名": "S.query",
                "平均耗时(ms)": "100",
                "调用次数": "2",
                "责任人": "张三",
                "处置状态": "待处理",
                "处置方案": "旧方案",
                "备注（原因）": "",
            },
            {
                "时间窗口结束": "2026-08-24 02:00:00",
                "应用英文名": "app-a",
                "服务名": "S.query",
                "平均耗时(ms)": "300.5",
                "调用次数": "3",
                "责任人": "",
                "处置状态": "已处理",
                "处置方案": "新方案",
                "备注（原因）": "最新备注",
            },
        ]

        items = aggregate_current_week("slow_service", rows)
        item = items[("app-a", "S.query")]

        self.assertEqual(item.value, "300.5 / 5")
        self.assertEqual(item.fixed.owner, "张三")
        self.assertEqual(item.fixed.status, "已处理")
        self.assertEqual(item.fixed.solution, "新方案")
        self.assertEqual(item.fixed.remark, "最新备注")

    def test_slow_sql_uses_max_latency_and_occurrence_count(self):
        rows = [
            {"应用英文名": "app-a", "SQL名": "M.query", "耗时(ms)": "100"},
            {"应用英文名": "app-a", "SQL名": "M.query", "耗时(ms)": "500"},
        ]

        item = aggregate_current_week("slow_sql", rows)[("app-a", "M.query")]

        self.assertEqual(item.value, "500 / 2")


class ScanSummaryPlanningTests(unittest.TestCase):
    def test_new_period_is_inserted_at_g_and_existing_fixed_fields_stay_unchanged(self):
        existing = [
            [*FIXED_HEADERS["slow_service"], "0817-0823", "0810-0816"],
            ["app-a", "S.query", "旧责任人", "旧状态", "旧方案", "旧备注", "100 / 1", "90 / 1"],
        ]
        current = [
            {
                "时间窗口结束": "2026-08-30 20:00:00",
                "应用英文名": "app-a",
                "服务名": "S.query",
                "平均耗时(ms)": "300",
                "调用次数": "2",
                "责任人": "新责任人",
                "处置状态": "新状态",
                "处置方案": "新方案",
                "备注（原因）": "新备注",
            },
            {
                "时间窗口结束": "2026-08-30 21:00:00",
                "应用英文名": "app-b",
                "服务名": "N.query",
                "平均耗时(ms)": "400",
                "调用次数": "1",
                "责任人": "李四",
                "处置状态": "待处理",
                "处置方案": "",
                "备注（原因）": "",
            },
        ]

        plan = plan_summary_update("slow_service", "0824-0830", current, existing)

        self.assertTrue(plan.insert_period)
        self.assertEqual(plan.period_col, 6)
        self.assertEqual(plan.value_updates[(1, 6)], "300 / 2")
        self.assertNotIn((1, 2), plan.value_updates)
        self.assertEqual(
            plan.append_rows,
            [["app-b", "N.query", "李四", "待处理", "", "", "400 / 1", "", ""]],
        )

    def test_existing_period_is_reused_without_another_column(self):
        existing = [
            [*FIXED_HEADERS["slow_sql"], "0824-0830", "0817-0823"],
            ["app-a", "M.query", "张三", "待处理", "", "", "100 / 1", "90 / 1"],
        ]
        current = [{"应用英文名": "app-a", "SQL名": "M.query", "耗时(ms)": "500"}]

        plan = plan_summary_update("slow_sql", "0824-0830", current, existing)

        self.assertFalse(plan.insert_period)
        self.assertEqual(plan.period_col, 6)
        self.assertEqual(plan.value_updates[(1, 6)], "500 / 1")

    def test_duplicate_existing_summary_key_fails_loudly(self):
        existing = [
            [*FIXED_HEADERS["slow_service"], "0817-0823"],
            ["app-a", "S.query", "", "", "", "", "100 / 1"],
            ["app-a", "S.query", "", "", "", "", "200 / 2"],
        ]

        with self.assertRaisesRegex(SummaryUpdateError, "重复唯一键"):
            plan_summary_update("slow_service", "0824-0830", [], existing)

    def test_duplicate_existing_period_header_fails_loudly(self):
        existing = [
            [*FIXED_HEADERS["slow_service"], "0817-0823", "0817-0823"],
        ]

        with self.assertRaisesRegex(SummaryUpdateError, "重复周期"):
            plan_summary_update("slow_service", "0817-0823", [], existing)


if __name__ == "__main__":
    unittest.main()
