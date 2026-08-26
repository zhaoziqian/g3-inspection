import csv
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.scan_summary import FIXED_HEADERS, plan_summary_update
from scripts.tencent_refresh_scan_summary import (
    SheetMeta,
    apply_update_plan,
    read_required_rows,
    read_summary_matrix,
    verify_applied_update,
)


class FakeClient:
    def __init__(self, matrices):
        self.matrices = matrices
        self.reads = []
        self.actions = []

    def get_csv(self, sheet_id, start_row, end_row, start_col, end_col):
        self.reads.append((sheet_id, start_row, end_row, start_col, end_col))
        source = self.matrices[sheet_id]
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        for row_index in range(start_row, min(end_row + 1, len(source))):
            row = source[row_index]
            writer.writerow([
                row[col] if col < len(row) else ""
                for col in range(start_col, end_col + 1)
            ])
        return stream.getvalue()

    def insert_dimension(self, *args, **kwargs):
        self.actions.append(("insert_dimension", args, kwargs))

    def clear_range(self, *args, **kwargs):
        self.actions.append(("clear_range", args, kwargs))

    def set_values(self, sheet_id, values):
        self.actions.append(("set_values", sheet_id, values))

    def style_period_column(self, *args, **kwargs):
        self.actions.append(("style_period_column", args, kwargs))


class TencentSummaryReaderTests(unittest.TestCase):
    def test_slow_sql_reader_never_reads_sql_statement_data_column(self):
        matrix = [
            ["时间窗口开始", "时间窗口结束", "应用英文名", "SQL名", "SQL语句", "耗时(ms)", "链路详情", "责任人", "处置状态", "处置方案", "备注（原因）"],
            ["start", "end", "app-a", "M.query", "SELECT secret", "500", "url", "张三", "待处理", "", ""],
        ]
        client = FakeClient({"sql": matrix})

        rows = read_required_rows(client, SheetMeta("sql", "慢SQL0824-0830", 2, 11), "slow_sql")

        self.assertEqual(rows[0]["SQL名"], "M.query")
        data_reads = [item for item in client.reads if item[1] > 0]
        self.assertTrue(data_reads)
        self.assertTrue(all(not (start_col <= 4 <= end_col) for _, _, _, start_col, end_col in data_reads))

    def test_summary_reader_trims_physical_empty_rows_and_columns(self):
        matrix = [
            [*FIXED_HEADERS["slow_service"], "0817-0823", "", ""],
            ["app-a", "S.query", "张三", "待处理", "", "", "100 / 1", "", ""],
            ["", "", "", "", "", "", "", "", ""],
        ]
        client = FakeClient({"summary": matrix})

        result = read_summary_matrix(client, SheetMeta("summary", "慢服务汇总", 200, 26))

        self.assertEqual(result, [matrix[0][:7], matrix[1][:7]])


class TencentSummaryApplyTests(unittest.TestCase):
    def test_apply_inserts_g_and_only_writes_existing_period_value_for_existing_key(self):
        existing = [
            [*FIXED_HEADERS["slow_service"], "0817-0823"],
            ["app-a", "S.query", "旧责任人", "旧状态", "旧方案", "旧备注", "100 / 1"],
        ]
        current = [{"应用英文名": "app-a", "服务名": "S.query", "平均耗时(ms)": "300", "调用次数": "2"}]
        plan = plan_summary_update("slow_service", "0824-0830", current, existing)
        client = FakeClient({})

        apply_update_plan(client, SheetMeta("summary", "慢服务汇总", 200, 26), len(existing), plan)

        insert = client.actions[0]
        self.assertEqual(insert[0], "insert_dimension")
        self.assertEqual(insert[2]["dimension_type"], "col")
        self.assertEqual(insert[2]["index"], 6)
        written = [action for action in client.actions if action[0] == "set_values"]
        flat = [cell for _name, _sheet, cells in written for cell in cells]
        self.assertIn({"row": 0, "col": 6, "value_type": "STRING", "string_value": "0824-0830"}, flat)
        self.assertIn({"row": 1, "col": 6, "value_type": "STRING", "string_value": "300 / 2"}, flat)
        self.assertFalse(any(cell["row"] == 1 and cell["col"] < 6 for cell in flat))

    def test_verification_rejects_any_existing_fixed_field_change(self):
        before = [
            [*FIXED_HEADERS["slow_service"], "0817-0823"],
            ["app-a", "S.query", "张三", "待处理", "方案", "备注", "100 / 1"],
        ]
        current = [{"应用英文名": "app-a", "服务名": "S.query", "平均耗时(ms)": "300", "调用次数": "2"}]
        plan = plan_summary_update("slow_service", "0824-0830", current, before)
        after = [
            [*FIXED_HEADERS["slow_service"], "0824-0830", "0817-0823"],
            ["app-a", "S.query", "被误改", "待处理", "方案", "备注", "300 / 2", "100 / 1"],
        ]

        with self.assertRaisesRegex(Exception, "固定字段"):
            verify_applied_update(before, after, plan)

    def test_verification_accepts_inserted_period_and_shifted_history(self):
        before = [
            [*FIXED_HEADERS["slow_service"], "0817-0823"],
            ["app-a", "S.query", "张三", "待处理", "方案", "备注", "100 / 1"],
        ]
        current = [{"应用英文名": "app-a", "服务名": "S.query", "平均耗时(ms)": "300", "调用次数": "2"}]
        plan = plan_summary_update("slow_service", "0824-0830", current, before)
        after = [
            [*FIXED_HEADERS["slow_service"], "0824-0830", "0817-0823"],
            ["app-a", "S.query", "张三", "待处理", "方案", "备注", "300 / 2", "100 / 1"],
        ]

        verify_applied_update(before, after, plan)


class TencentSummaryCliTests(unittest.TestCase):
    def test_absolute_script_path_runs_outside_skill_repository(self):
        script = Path(__file__).parents[1] / "scripts" / "tencent_refresh_scan_summary.py"
        with tempfile.TemporaryDirectory() as workdir:
            result = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=workdir,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
