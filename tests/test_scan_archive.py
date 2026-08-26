import unittest

from scripts.scan_archive import (
    ArchiveError,
    ARCHIVE_HEADERS,
    index_archive_blocks,
    normalize_source_rows,
    parse_week_sheet_name,
    plan_period_action,
    row_digest,
    select_archive_periods,
)


class WeekSheetSelectionTests(unittest.TestCase):
    def test_parse_accepts_only_canonical_week_sheet_names(self):
        self.assertEqual(parse_week_sheet_name("慢服务0518-0525"), ("slow_service", "0518-0525"))
        self.assertEqual(parse_week_sheet_name("慢SQL0518-0525"), ("slow_sql", "0518-0525"))
        for name in (
            "慢SQL0525-0531（作废）",
            "慢服务汇总",
            "慢SQL归档",
            "慢服务0518_0525",
            "前缀慢服务0518-0525",
        ):
            self.assertIsNone(parse_week_sheet_name(name), name)

    def test_selects_only_paired_periods_and_keeps_latest_five(self):
        names = []
        periods = [
            "0518-0525", "0525-0531", "0601-0607", "0608-0614",
            "0615-0621", "0622-0628", "0629-0705", "0706-0712",
            "0713-0719", "0720-0726", "0727-0802", "0803-0809",
            "0810-0816", "0817-0823",
        ]
        for period in periods:
            names.extend((f"慢服务{period}", f"慢SQL{period}"))
        names.extend(("慢服务0824-0830", "慢SQL0525-0531（作废）"))

        result = select_archive_periods(names, keep_weeks=5)

        self.assertEqual(result.keep, tuple(periods[-5:]))
        self.assertEqual(result.archive, tuple(periods[:-5]))
        self.assertEqual(result.unpaired, ("0824-0830",))

    def test_cross_year_periods_are_ordered_chronologically(self):
        names = [
            "慢服务1221-1227", "慢SQL1221-1227",
            "慢服务1228-0103", "慢SQL1228-0103",
            "慢服务0104-0110", "慢SQL0104-0110",
            "慢服务0111-0117", "慢SQL0111-0117",
        ]

        result = select_archive_periods(names, keep_weeks=2)

        self.assertEqual(result.archive, ("1221-1227", "1228-0103"))
        self.assertEqual(result.keep, ("0104-0110", "0111-0117"))


class SourceNormalizationTests(unittest.TestCase):
    def test_slow_service_maps_reordered_headers(self):
        header = [
            "服务名", "应用英文名", "调用次数", "平均耗时(ms)", "时间窗口开始",
            "时间窗口结束", "链路详情", "责任人", "处置状态", "处置方案", "备注（原因）",
        ]
        rows = [["svc", "app", "2", "300", "start", "end", "trace", "张三", "待处理", "方案", "备注"]]

        normalized = normalize_source_rows("slow_service", "0518-0525", header, rows)

        self.assertEqual(normalized, [["0518-0525", "start", "end", "app", "svc", "300", "2", "trace", "张三", "待处理", "方案", "备注"]])

    def test_historical_slow_sql_without_trace_column_gets_blank_trace(self):
        header = [
            "时间窗口开始", "时间窗口结束", "应用英文名", "SQL名", "SQL语句",
            "耗时(ms)", "责任人", "处置状态", "处置方案", "备注（原因）",
        ]
        rows = [["start", "end", "app", "mapper", "select 1", "900", "李四", "完成", "优化", "ok"]]

        normalized = normalize_source_rows("slow_sql", "0525-0531", header, rows)

        self.assertEqual(normalized[0], ["0525-0531", "start", "end", "app", "mapper", "select 1", "900", "", "李四", "完成", "优化", "ok"])

    def test_missing_required_header_fails_loudly(self):
        header = [name for name in ARCHIVE_HEADERS["slow_sql"][1:] if name != "SQL语句"]

        with self.assertRaisesRegex(ArchiveError, "SQL语句"):
            normalize_source_rows("slow_sql", "0518-0525", header, [])


class ArchiveBlockTests(unittest.TestCase):
    def test_row_digest_is_stable_and_sensitive_to_empty_cells(self):
        self.assertEqual(row_digest(["a", "", "b"]), row_digest(["a", "", "b"]))
        self.assertNotEqual(row_digest(["a", "", "b"]), row_digest(["a", "b", ""]))

    def test_indexes_contiguous_period_blocks(self):
        rows = [
            ["0518-0525", "a"],
            ["0518-0525", "b"],
            ["0525-0531", "c"],
        ]

        blocks = index_archive_blocks(rows)

        self.assertEqual((blocks["0518-0525"].start, blocks["0518-0525"].end), (0, 2))
        self.assertEqual((blocks["0525-0531"].start, blocks["0525-0531"].end), (2, 3))

    def test_rejects_noncontiguous_period_blocks(self):
        with self.assertRaisesRegex(ArchiveError, "不连续"):
            index_archive_blocks([
                ["0518-0525", "a"],
                ["0525-0531", "b"],
                ["0518-0525", "c"],
            ])

    def test_plan_period_action_distinguishes_skip_replace_append_and_fail(self):
        source = [["0518-0525", "a"], ["0518-0525", "b"]]
        self.assertEqual(plan_period_action(source, source, source_exists=True), "skip")
        self.assertEqual(plan_period_action(source, source[:1], source_exists=True), "replace")
        self.assertEqual(plan_period_action(source, [], source_exists=True), "append")
        self.assertEqual(plan_period_action(source, source, source_exists=False), "skip")
        with self.assertRaisesRegex(ArchiveError, "源 Sheet 已不存在"):
            plan_period_action(source, source[:1], source_exists=False)


if __name__ == "__main__":
    unittest.main()
