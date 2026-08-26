import unittest
from unittest.mock import patch

from scripts.tencent_upload_scan_reports import (
    UploadDirectoryClient,
    refresh_directory_after_upload,
)


def six_week_sheet_info():
    fixed = [
        {"sheet_id": "BB08J2", "sheet_name": "目录", "row_count": 197, "col_count": 26},
        {"sheet_id": "z776s9", "sheet_name": "慢服务汇总", "row_count": 238, "col_count": 26},
        {"sheet_id": "cczg56", "sheet_name": "慢SQL汇总", "row_count": 200, "col_count": 26},
        {"sheet_id": "7i67j3", "sheet_name": "慢服务归档", "row_count": 3024, "col_count": 26},
        {"sheet_id": "8i29ez", "sheet_name": "慢SQL归档", "row_count": 2942, "col_count": 26},
    ]
    periods = (
        "0720-0726",
        "0727-0802",
        "0803-0809",
        "0810-0816",
        "0817-0823",
        "0824-0830",
    )
    weekly = [
        {
            "sheet_id": f"{kind}-{period}",
            "sheet_name": f"{prefix}{period}",
            "row_count": 200,
            "col_count": 26,
        }
        for period in periods
        for kind, prefix in (("svc", "慢服务"), ("sql", "慢SQL"))
    ]
    return [*fixed, *weekly]


class SheetEngineDouble:
    def __init__(self):
        self.sheets = six_week_sheet_info()
        self.values = []
        self.links = {}

    def __call__(self, tool, arguments):
        if tool == "get_sheet_info":
            return {"sheets": self.sheets}
        if tool == "set_range_value":
            self.values = [dict(cell) for cell in arguments["values"]]
            return {}
        if tool == "set_link":
            key = (arguments["row"], arguments["col"])
            self.links[key] = {
                "text": arguments["display_text"],
                "url": arguments["url"].rsplit("=", 1)[-1],
            }
            return {}
        if tool == "get_cell_data":
            cells = []
            for value in self.values:
                row = value["row"]
                col = value["col"]
                cell = {
                    "row": row,
                    "col": col,
                    "string_value": value.get("string_value", ""),
                    "hyperlinks": [],
                }
                if (row, col) in self.links:
                    cell["hyperlinks"] = [dict(self.links[(row, col)])]
                cells.append(cell)
            return {"cells": cells}
        raise AssertionError(tool)


class UploadDirectoryIntegrationTests(unittest.TestCase):
    @patch("scripts.tencent_upload_scan_reports.sheetengine")
    def test_refresh_rolls_six_weeks_to_fixed_latest_five_navigation(self, sheetengine):
        engine = SheetEngineDouble()
        sheetengine.side_effect = engine

        plan = refresh_directory_after_upload()

        self.assertEqual(
            [row[0] for row in plan.rows[2:7]],
            ["0727-0802", "0803-0809", "0810-0816", "0817-0823", "0824-0830"],
        )
        self.assertEqual(len(engine.values), 24)
        self.assertEqual(len(engine.links), 14)
        self.assertEqual(engine.links[(7, 13)]["text"], "慢服务归档")
        self.assertEqual(engine.links[(7, 14)]["url"], "8i29ez")

    @patch("scripts.tencent_upload_scan_reports.sheetengine")
    def test_adapter_preserves_bounded_cell_coordinates(self, sheetengine):
        sheetengine.return_value = {"cells": [{"row": 0, "col": 12, "string_value": "周期"}]}
        client = UploadDirectoryClient()

        cells = client.get_cells("BB08J2", 0, 7, 12, 14)

        self.assertEqual(cells, [{"row": 0, "col": 12, "string_value": "周期"}])
        tool, arguments = sheetengine.call_args.args
        self.assertEqual(tool, "get_cell_data")
        self.assertEqual(
            {key: arguments[key] for key in ("sheet_id", "start_row", "end_row", "start_col", "end_col")},
            {"sheet_id": "BB08J2", "start_row": 0, "end_row": 7, "start_col": 12, "end_col": 14},
        )


if __name__ == "__main__":
    unittest.main()
