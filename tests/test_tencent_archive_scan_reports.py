import csv
import base64
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.scan_archive import ARCHIVE_HEADERS, ArchiveError
from scripts.tencent_archive_scan_reports import (
    SheetMeta,
    TencentArchiveClient,
    apply_archive_sheet,
    build_dry_run,
    make_row_cells,
    parse_args,
    read_csv_adaptive,
    read_source_sheet,
    verify_archive_pair,
)


SVC_HEADER = ARCHIVE_HEADERS["slow_service"][1:]
SQL_HEADER = ARCHIVE_HEADERS["slow_sql"][1:]


def as_csv(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows(rows)
    return stream.getvalue()


class MatrixClient:
    def __init__(self, sheets, matrices):
        self._sheets = sheets
        self.matrices = matrices
        self.reads = []

    def sheet_info(self):
        return self._sheets

    def get_csv(self, sheet_id, start_row, end_row, start_col, end_col):
        self.reads.append((sheet_id, start_row, end_row, start_col, end_col))
        matrix = self.matrices[sheet_id]
        rows = []
        for row_index in range(start_row, end_row + 1):
            source = matrix[row_index] if row_index < len(matrix) else []
            rows.append([source[col] if col < len(source) else "" for col in range(start_col, end_col + 1)])
        return as_csv(rows)


class WriteClient:
    def __init__(self):
        self.actions = []

    def insert_dimension(self, sheet_id, **arguments):
        self.actions.append(("insert_dimension", sheet_id, arguments))

    def delete_dimension(self, sheet_id, **arguments):
        self.actions.append(("delete_dimension", sheet_id, arguments))

    def clear_range(self, sheet_id, **arguments):
        self.actions.append(("clear_range", sheet_id, arguments))

    def set_values(self, sheet_id, values):
        self.actions.append(("set_values", sheet_id, values))

    def style_archive(self, sheet_id, end_row, end_col):
        self.actions.append(("style_archive", sheet_id, end_row, end_col))


class AdaptiveReadTests(unittest.TestCase):
    def test_invalid_multirow_response_is_bisected_without_skipping_rows(self):
        class TruncatingClient:
            def __init__(self):
                self.calls = []

            def get_csv(self, sheet_id, start_row, end_row, start_col, end_col):
                self.calls.append((start_row, end_row))
                if end_row > start_row:
                    raise ArchiveError("返回无效 JSON")
                return as_csv([[f"sql-{start_row}"]])

        client = TruncatingClient()

        values = read_csv_adaptive(client, "sql", 1, 4, 4, 4)

        self.assertEqual(values, [["sql-1"], ["sql-2"], ["sql-3"], ["sql-4"]])
        self.assertIn((1, 4), client.calls)
        self.assertIn((1, 2), client.calls)
        self.assertIn((1, 1), client.calls)

    def test_single_unreadable_sql_row_fails_with_sheet_and_row(self):
        class BrokenClient:
            def get_csv(self, sheet_id, start_row, end_row, start_col, end_col):
                raise ArchiveError("返回无效 JSON")

        with self.assertRaisesRegex(ArchiveError, "慢SQL0518-0525.*第 2 行"):
            read_csv_adaptive(BrokenClient(), "sql-id", 1, 1, 4, 4, sheet_name="慢SQL0518-0525")

    def test_source_reader_preserves_long_sql_and_historical_blank_trace(self):
        long_sql = "SELECT 'x'\nFROM dual " * 1200
        old_header = [name for name in SQL_HEADER if name != "链路详情"]
        old_row = ["start", "end", "app", "mapper", long_sql, "900", "李四", "完成", "方案", "备注"]
        client = MatrixClient([], {"sql": [old_header, old_row]})

        rows = read_source_sheet(client, SheetMeta("sql", "慢SQL0525-0531", 2, 10), "slow_sql", "0525-0531")

        self.assertEqual(rows[0][5], long_sql)
        self.assertEqual(rows[0][7], "")

    def test_source_reader_rejects_missing_required_header(self):
        header = [name for name in SQL_HEADER if name != "SQL语句"]
        client = MatrixClient([], {"sql": [header]})

        with self.assertRaisesRegex(ArchiveError, "SQL语句"):
            read_source_sheet(client, SheetMeta("sql", "慢SQL0525-0531", 1, 10), "slow_sql", "0525-0531")

    def test_source_reader_groups_contiguous_non_sql_columns(self):
        row = ["start", "end", "app", "mapper", "SELECT 1", "900", "trace", "李四", "完成", "方案", "备注"]
        client = MatrixClient([], {"sql": [SQL_HEADER, row]})

        read_source_sheet(client, SheetMeta("sql", "慢SQL0518-0525", 2, 11), "slow_sql", "0518-0525")

        data_reads = [read for read in client.reads if read[1] == 1]
        self.assertEqual(data_reads, [
            ("sql", 1, 1, 0, 3),
            ("sql", 1, 1, 5, 10),
            ("sql", 1, 1, 4, 4),
        ])


class DryRunTests(unittest.TestCase):
    def test_default_cli_mode_is_dry_run_and_keep_weeks_is_five(self):
        args = parse_args([])
        self.assertFalse(args.apply)
        self.assertEqual(args.keep_weeks, 5)

    def test_build_dry_run_reads_both_sources_before_planning_delete(self):
        periods = ["0518-0525", "0525-0531", "0601-0607", "0608-0614", "0615-0621", "0622-0628"]
        sheets = [
            {"sheet_id": "svc-archive", "sheet_name": "慢服务归档", "row_count": 20, "col_count": 12},
            {"sheet_id": "sql-archive", "sheet_name": "慢SQL归档", "row_count": 20, "col_count": 12},
            {"sheet_id": "directory", "sheet_name": "目录", "row_count": 20, "col_count": 26},
        ]
        matrices = {
            "svc-archive": [[]],
            "sql-archive": [[]],
            "directory": [[""] * 26, *[[""] * 12 + [period, f"慢服务{period}", f"慢SQL{period}"] for period in periods]],
        }
        for period in periods:
            svc_id = f"svc-{period}"
            sql_id = f"sql-{period}"
            sheets.extend((
                {"sheet_id": svc_id, "sheet_name": f"慢服务{period}", "row_count": 2, "col_count": 11},
                {"sheet_id": sql_id, "sheet_name": f"慢SQL{period}", "row_count": 2, "col_count": 11},
            ))
            matrices[svc_id] = [SVC_HEADER, ["start", "end", "app", "svc", "300", "2", "trace", "张三", "待处理", "", ""]]
            matrices[sql_id] = [SQL_HEADER, ["start", "end", "app", "mapper", "SELECT 1", "900", "trace", "李四", "待处理", "", ""]]
        client = MatrixClient(sheets, matrices)

        report = build_dry_run(
            client,
            keep_weeks=5,
            targets={"slow_service": ("慢服务归档", "svc-archive"), "slow_sql": ("慢SQL归档", "sql-archive")},
            directory_id="directory",
        )

        self.assertEqual(report["archive_periods"], ["0518-0525"])
        self.assertEqual(report["keep_periods"], periods[-5:])
        self.assertEqual(report["slow_service"]["0518-0525"]["source_rows"], 1)
        self.assertEqual(report["slow_sql"]["0518-0525"]["source_rows"], 1)
        self.assertEqual(report["delete_sheets"], ["慢服务0518-0525", "慢SQL0518-0525"])
        self.assertEqual(report["directory_rows"], {"0518-0525": 1})


class ArchiveWriteTests(unittest.TestCase):
    def test_sql_statement_cells_use_base64_without_plaintext(self):
        row = ["0518-0525", "start", "end", "app", "mapper", "SELECT * FROM secret", "900", "", "", "", "", ""]

        cells = make_row_cells("slow_sql", 1, row)

        sql_cell = cells[5]
        self.assertNotIn("string_value", sql_cell)
        self.assertEqual(base64.b64decode(sql_cell["value_base64"]).decode("utf-8"), row[5])
        self.assertEqual(cells[4]["string_value"], "mapper")

    def test_empty_archive_writes_header_and_rows_and_expands_capacity(self):
        client = WriteClient()
        rows = [
            ["0518-0525", "start", "end", "app", "svc-a", "300", "2", "", "", "", "", ""],
            ["0518-0525", "start", "end", "app", "svc-b", "400", "1", "", "", "", "", ""],
        ]

        expected = apply_archive_sheet(
            client,
            SheetMeta("archive", "慢服务归档", 1, 12),
            "slow_service",
            [],
            {"0518-0525": rows},
        )

        self.assertEqual(expected, rows)
        insert = next(action for action in client.actions if action[0] == "insert_dimension")
        self.assertEqual(insert[2], {"dimension_type": "row", "index": 0, "count": 2, "direction": "after"})
        written = [cell for action in client.actions if action[0] == "set_values" for cell in action[2]]
        self.assertIn({"row": 0, "col": 0, "value_type": "STRING", "string_value": "周期"}, written)
        self.assertIn({"row": 2, "col": 4, "value_type": "STRING", "string_value": "svc-b"}, written)
        self.assertEqual(client.actions[-1], ("style_archive", "archive", 2, 11))

    def test_mismatched_existing_period_replaces_only_its_dimension_block(self):
        client = WriteClient()
        current = [
            ["0518-0525", "old-a"],
            ["0518-0525", "old-b"],
            ["0525-0531", "keep"],
        ]
        replacement = [["0518-0525", "new"]]

        expected = apply_archive_sheet(
            client,
            SheetMeta("archive", "慢服务归档", 20, 12),
            "slow_service",
            current,
            {"0518-0525": replacement},
        )

        self.assertEqual(expected, [replacement[0], current[2]])
        delete = next(action for action in client.actions if action[0] == "delete_dimension")
        self.assertEqual(delete[2], {"dimension_type": "row", "index": 1, "count": 2})
        insert = next(action for action in client.actions if action[0] == "insert_dimension")
        self.assertEqual(insert[2], {"dimension_type": "row", "index": 1, "count": 1, "direction": "before"})

    def test_verify_archive_pair_rejects_any_full_row_hash_mismatch(self):
        svc_rows = [["0518-0525", "start", "end", "app", "svc", "300", "2", "", "", "", "", ""]]
        sql_rows = [["0518-0525", "start", "end", "app", "mapper", "SELECT 1", "900", "", "", "", "", ""]]
        sheets = [
            {"sheet_id": "svc", "sheet_name": "慢服务归档", "row_count": 2, "col_count": 12},
            {"sheet_id": "sql", "sheet_name": "慢SQL归档", "row_count": 2, "col_count": 12},
        ]
        matrices = {
            "svc": [list(ARCHIVE_HEADERS["slow_service"]), *[row[:] for row in svc_rows]],
            "sql": [list(ARCHIVE_HEADERS["slow_sql"]), *[row[:] for row in sql_rows]],
        }
        client = MatrixClient(sheets, matrices)
        metas = {
            "slow_service": SheetMeta("svc", "慢服务归档", 2, 12),
            "slow_sql": SheetMeta("sql", "慢SQL归档", 2, 12),
        }

        verify_archive_pair(client, metas, {"slow_service": svc_rows, "slow_sql": sql_rows})
        matrices["sql"][1][6] = "901"
        with self.assertRaisesRegex(ArchiveError, "内容不一致"):
            verify_archive_pair(client, metas, {"slow_service": svc_rows, "slow_sql": sql_rows})


class TencentArchiveClientTests(unittest.TestCase):
    def test_absolute_script_path_runs_outside_skill_repository(self):
        script = Path(__file__).parents[1] / "scripts" / "tencent_archive_scan_reports.py"
        with tempfile.TemporaryDirectory() as workdir:
            result = subprocess.run([sys.executable, str(script), "--help"], cwd=workdir, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @patch("scripts.tencent_archive_scan_reports.subprocess.run")
    def test_client_rejects_business_error_json(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = '{"error":"permission denied"}'
        run.return_value.stderr = ""
        with self.assertRaisesRegex(ArchiveError, "permission denied"):
            TencentArchiveClient().sheet_info()


if __name__ == "__main__":
    unittest.main()
