import csv
import base64
import io
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.scan_archive import ARCHIVE_HEADERS, ArchiveError
from scripts.tencent_archive_scan_reports import (
    SheetMeta,
    TencentArchiveClient,
    apply_archive_sheet,
    build_dry_run,
    delete_verified_sources,
    make_row_cells,
    parse_args,
    read_csv_adaptive,
    read_source_sheet,
    run_archive,
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

    def test_sql_column_starts_with_fifty_rows_then_adapts_on_failure(self):
        rows = [["start", "end", "app", f"mapper-{index}", f"SELECT {index}", "900", "", "", "", "", ""] for index in range(51)]
        client = MatrixClient([], {"sql": [SQL_HEADER, *rows]})

        read_source_sheet(client, SheetMeta("sql", "慢SQL0518-0525", 52, 11), "slow_sql", "0518-0525")

        sql_reads = [read for read in client.reads if read[3:] == (4, 4)]
        self.assertEqual(sql_reads, [("sql", 1, 50, 4, 4), ("sql", 51, 51, 4, 4)])


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
        self.assertEqual(report["directory_preview"], periods[-5:])

    def test_partial_deletion_is_recovered_from_verified_archive_blocks(self):
        archived_svc = ["0518-0525", "start", "end", "app", "svc", "300", "2", "", "", "", "", ""]
        archived_sql = ["0518-0525", "start", "end", "app", "mapper", "SELECT 1", "900", "", "", "", "", ""]
        keep = ["0525-0531", "0601-0607", "0608-0614", "0615-0621", "0622-0628"]
        sheets = [
            {"sheet_id": "svc-archive", "sheet_name": "慢服务归档", "row_count": 2, "col_count": 12},
            {"sheet_id": "sql-archive", "sheet_name": "慢SQL归档", "row_count": 2, "col_count": 12},
            {"sheet_id": "directory", "sheet_name": "目录", "row_count": 10, "col_count": 26},
            {"sheet_id": "svc-old", "sheet_name": "慢服务0518-0525", "row_count": 2, "col_count": 11},
        ]
        matrices = {
            "svc-archive": [ARCHIVE_HEADERS["slow_service"], archived_svc],
            "sql-archive": [ARCHIVE_HEADERS["slow_sql"], archived_sql],
            "directory": [[""] * 26],
            "svc-old": [SVC_HEADER, archived_svc[1:]],
        }
        for period in keep:
            for kind, prefix, header, row in (
                ("svc", "慢服务", SVC_HEADER, archived_svc[1:]),
                ("sql", "慢SQL", SQL_HEADER, archived_sql[1:]),
            ):
                sheet_id = f"{kind}-{period}"
                sheets.append({"sheet_id": sheet_id, "sheet_name": f"{prefix}{period}", "row_count": 2, "col_count": 11})
                matrices[sheet_id] = [header, row]
        client = MatrixClient(sheets, matrices)

        report = build_dry_run(
            client,
            targets={"slow_service": ("慢服务归档", "svc-archive"), "slow_sql": ("慢SQL归档", "sql-archive")},
            directory_id="directory",
        )

        self.assertEqual(report["archive_periods"], ["0518-0525"])
        self.assertEqual(report["recovery_periods"], ["0518-0525"])
        self.assertEqual(report["delete_sheets"], ["慢服务0518-0525"])
        self.assertEqual(report["slow_sql"]["0518-0525"]["action"], "skip")


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


class DestructiveGateTests(unittest.TestCase):
    def test_source_deletion_is_blocked_until_both_archives_are_verified(self):
        class DeleteClient:
            def __init__(self):
                self.deleted = []

            def delete_sheet(self, sheet_id):
                self.deleted.append(sheet_id)

            def sheet_info(self):
                return []

        client = DeleteClient()
        sources = {
            "slow_service": {"0518-0525": SheetMeta("svc", "慢服务0518-0525", 2, 11)},
            "slow_sql": {"0518-0525": SheetMeta("sql", "慢SQL0518-0525", 2, 11)},
        }

        with self.assertRaisesRegex(ArchiveError, "尚未通过双归档校验"):
            delete_verified_sources(client, sources, ["0518-0525"], archives_verified=False)
        self.assertEqual(client.deleted, [])

    def test_source_deletion_uses_exact_ids_and_confirms_absence(self):
        class DeleteClient:
            def __init__(self):
                self.sheets = [
                    {"sheet_id": "svc", "sheet_name": "慢服务0518-0525"},
                    {"sheet_id": "sql", "sheet_name": "慢SQL0518-0525"},
                    {"sheet_id": "obsolete", "sheet_name": "慢SQL0525-0531（作废）"},
                ]
                self.deleted = []

            def delete_sheet(self, sheet_id):
                self.deleted.append(sheet_id)
                self.sheets = [sheet for sheet in self.sheets if sheet["sheet_id"] != sheet_id]

            def sheet_info(self):
                return self.sheets

        client = DeleteClient()
        sources = {
            "slow_service": {"0518-0525": SheetMeta("svc", "慢服务0518-0525", 2, 11)},
            "slow_sql": {"0518-0525": SheetMeta("sql", "慢SQL0518-0525", 2, 11)},
        }

        deleted = delete_verified_sources(client, sources, ["0518-0525"], archives_verified=True)

        self.assertEqual(deleted, ["慢服务0518-0525", "慢SQL0518-0525"])
        self.assertEqual(client.deleted, ["svc", "sql"])
        self.assertEqual(client.sheets, [{"sheet_id": "obsolete", "sheet_name": "慢SQL0525-0531（作废）"}])

    @patch("scripts.tencent_archive_scan_reports.prepare_archive")
    @patch("scripts.tencent_archive_scan_reports.apply_archive_sheet")
    @patch("scripts.tencent_archive_scan_reports.verify_archive_pair")
    @patch("scripts.tencent_archive_scan_reports.delete_verified_sources")
    @patch("scripts.tencent_archive_scan_reports.rebuild_directory_navigation")
    def test_directory_rebuild_runs_only_after_verified_source_deletion(
        self, rebuild, delete_sources, verify, apply_sheet, prepare
    ):
        events = []
        final_sheets = [
            {"sheet_id": "BB08J2", "sheet_name": "目录", "row_count": 8, "col_count": 26},
            {"sheet_id": "svc-a", "sheet_name": "慢服务归档", "row_count": 2, "col_count": 12},
            {"sheet_id": "sql-a", "sheet_name": "慢SQL归档", "row_count": 2, "col_count": 12},
        ]

        class Client:
            def sheet_info(self):
                events.append("sheet_info")
                return final_sheets

        prepare.return_value = SimpleNamespace(
            report={},
            periods=("0518-0525",),
            target_metas={
                "slow_service": SheetMeta("svc-a", "慢服务归档", 2, 12),
                "slow_sql": SheetMeta("sql-a", "慢SQL归档", 2, 12),
            },
            current_rows={"slow_service": [], "slow_sql": []},
            sources={"slow_service": {"0518-0525": []}, "slow_sql": {"0518-0525": []}},
            source_metas={"slow_service": {}, "slow_sql": {}},
        )
        apply_sheet.side_effect = [[], []]
        verify.side_effect = lambda *args: events.append("verify")
        delete_sources.side_effect = lambda *args, **kwargs: events.append("delete") or []
        rebuild.side_effect = lambda *args: events.append("rebuild")
        client = Client()

        report = run_archive(client, apply=True)

        self.assertLess(events.index("verify"), events.index("delete"))
        self.assertLess(events.index("delete"), events.index("rebuild"))
        rebuild.assert_called_once_with(client, final_sheets)
        self.assertTrue(report["directory_rebuilt"])

    @patch("scripts.tencent_archive_scan_reports.rebuild_directory_navigation")
    @patch("scripts.tencent_archive_scan_reports.prepare_archive")
    @patch("scripts.tencent_archive_scan_reports.apply_archive_sheet")
    @patch("scripts.tencent_archive_scan_reports.verify_archive_pair")
    def test_pipeline_never_deletes_when_archive_verification_fails(
        self, verify, apply_sheet, prepare, rebuild
    ):
        class Client:
            def __init__(self):
                self.deleted = []

            def sheet_info(self):
                return [
                    {"sheet_id": "svc-a", "sheet_name": "慢服务归档", "row_count": 2, "col_count": 12},
                    {"sheet_id": "sql-a", "sheet_name": "慢SQL归档", "row_count": 2, "col_count": 12},
                ]

            def delete_sheet(self, sheet_id):
                self.deleted.append(sheet_id)

        prepare.return_value = SimpleNamespace(
            report={},
            periods=("0518-0525",),
            target_metas={
                "slow_service": SheetMeta("svc-a", "慢服务归档", 2, 12),
                "slow_sql": SheetMeta("sql-a", "慢SQL归档", 2, 12),
            },
            current_rows={"slow_service": [], "slow_sql": []},
            sources={"slow_service": {"0518-0525": []}, "slow_sql": {"0518-0525": []}},
            source_metas={"slow_service": {}, "slow_sql": {}},
        )
        apply_sheet.side_effect = [[], []]
        verify.side_effect = ArchiveError("内容不一致")
        client = Client()

        with self.assertRaisesRegex(ArchiveError, "内容不一致"):
            run_archive(client, apply=True)

        self.assertEqual(client.deleted, [])
        rebuild.assert_not_called()


class TencentArchiveClientTests(unittest.TestCase):
    def test_directory_protocol_methods_forward_structured_cells_and_links(self):
        class RecordingClient(TencentArchiveClient):
            def __init__(self):
                self.calls = []

            def call(self, tool, arguments):
                self.calls.append((tool, arguments))
                return {"cells": [{"row": 0, "col": 12, "string_value": "周期"}]}

        client = RecordingClient()

        cells = client.get_cells("BB08J2", 0, 7, 12, 14)
        client.set_link("BB08J2", 1, 13, "https://docs.qq.com/sheet/id?tab=svc", "慢服务汇总")

        self.assertEqual(cells, [{"row": 0, "col": 12, "string_value": "周期"}])
        self.assertEqual(
            client.calls,
            [
                ("get_cell_data", {
                    "sheet_id": "BB08J2",
                    "start_row": 0,
                    "end_row": 7,
                    "start_col": 12,
                    "end_col": 14,
                }),
                ("set_link", {
                    "sheet_id": "BB08J2",
                    "row": 1,
                    "col": 13,
                    "url": "https://docs.qq.com/sheet/id?tab=svc",
                    "display_text": "慢服务汇总",
                }),
            ],
        )

    def test_archive_styling_tolerates_remove_filter_when_sheet_has_no_filter(self):
        class NoFilterClient(TencentArchiveClient):
            def __init__(self):
                self.calls = []

            def call(self, tool, arguments):
                self.calls.append((tool, arguments))
                if tool == "remove_filter":
                    raise ArchiveError("code: 12002, msg: remove sheet filter, but sheet has no filter")
                return {}

        client = NoFilterClient()

        client.style_archive("archive", 0, 11)

        self.assertIn(
            ("set_filter", {
                "sheet_id": "archive",
                "filter_id": "g3_archive_archive",
                "start_row": 0,
                "end_row": 0,
                "start_col": 0,
                "end_col": 11,
            }),
            client.calls,
        )

    def test_archive_styling_propagates_unrelated_remove_filter_errors(self):
        class BrokenFilterClient(TencentArchiveClient):
            def call(self, tool, arguments):
                if tool == "remove_filter":
                    raise ArchiveError("permission denied")
                return {}

        with self.assertRaisesRegex(ArchiveError, "permission denied"):
            BrokenFilterClient().style_archive("archive", 0, 11)

    def test_absolute_script_path_runs_outside_skill_repository(self):
        script = Path(__file__).parents[1] / "scripts" / "tencent_archive_scan_reports.py"
        with tempfile.TemporaryDirectory() as workdir:
            result = subprocess.run([sys.executable, str(script), "--help"], cwd=workdir, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @patch("scripts.tencent_archive_scan_reports.subprocess.run")
    def test_client_rejects_business_error_json(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = b'{"error":"permission denied"}'
        run.return_value.stderr = b""
        with self.assertRaisesRegex(ArchiveError, "permission denied"):
            TencentArchiveClient().sheet_info()

    @patch("scripts.tencent_archive_scan_reports.subprocess.run")
    def test_client_reports_tool_and_return_code_for_truncated_utf8_output(self, run):
        run.return_value.returncode = 1
        run.return_value.stdout = b""
        run.return_value.stderr = b"partial \xe6"

        with self.assertRaisesRegex(ArchiveError, "set_range_value.*returncode=1.*9 bytes"):
            TencentArchiveClient().set_values("archive", [])

    @patch("scripts.tencent_archive_scan_reports.subprocess.run")
    def test_client_requests_explicit_json_output_and_long_read_timeout(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = b'{"sheets":[]}'
        run.return_value.stderr = b""

        TencentArchiveClient().sheet_info()

        command = run.call_args.args[0]
        self.assertIn("--output", command)
        self.assertEqual(command[command.index("--output") + 1], "json")
        self.assertIn("--timeout", command)
        self.assertEqual(command[command.index("--timeout") + 1], "180000")


if __name__ == "__main__":
    unittest.main()
