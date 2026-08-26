import unittest

from scripts.directory_navigation import (
    NavigationError,
    build_navigation_plan,
    rebuild_directory_navigation,
)


FIXED_SHEETS = [
    {"sheet_id": "BB08J2", "sheet_name": "目录", "row_count": 197, "col_count": 26},
    {"sheet_id": "z776s9", "sheet_name": "慢服务汇总", "row_count": 238, "col_count": 26},
    {"sheet_id": "cczg56", "sheet_name": "慢SQL汇总", "row_count": 200, "col_count": 26},
    {"sheet_id": "7i67j3", "sheet_name": "慢服务归档", "row_count": 3024, "col_count": 26},
    {"sheet_id": "8i29ez", "sheet_name": "慢SQL归档", "row_count": 2942, "col_count": 26},
]
SIX_PERIODS = (
    "0720-0726",
    "0727-0802",
    "0803-0809",
    "0810-0816",
    "0817-0823",
    "0824-0830",
)


def paired_sheets(periods=SIX_PERIODS):
    return [
        {
            "sheet_id": f"{kind}-{period}",
            "sheet_name": f"{prefix}{period}",
            "row_count": 200,
            "col_count": 26,
        }
        for period in periods
        for kind, prefix in (("svc", "慢服务"), ("sql", "慢SQL"))
    ]


def six_week_sheets():
    return [
        *FIXED_SHEETS,
        *paired_sheets(),
        {"sheet_id": "obsolete", "sheet_name": "慢SQL0525-0531（作废）", "row_count": 10, "col_count": 26},
        {"sheet_id": "business", "sheet_name": "首页", "row_count": 200, "col_count": 26},
    ]


class DirectoryNavigationPlanTests(unittest.TestCase):
    def test_keeps_latest_five_paired_weeks_oldest_to_newest(self):
        plan = build_navigation_plan(six_week_sheets())

        self.assertEqual(
            plan.rows,
            (
                ("周期", "慢服务扫描报告", "慢SQL扫描报告"),
                ("", "慢服务汇总", "慢SQL汇总"),
                ("0727-0802", "慢服务0727-0802", "慢SQL0727-0802"),
                ("0803-0809", "慢服务0803-0809", "慢SQL0803-0809"),
                ("0810-0816", "慢服务0810-0816", "慢SQL0810-0816"),
                ("0817-0823", "慢服务0817-0823", "慢SQL0817-0823"),
                ("0824-0830", "慢服务0824-0830", "慢SQL0824-0830"),
                ("", "慢服务归档", "慢SQL归档"),
            ),
        )
        self.assertEqual(
            {(link.row, link.col) for link in plan.links},
            {(row, col) for row in range(1, 8) for col in (13, 14)},
        )
        self.assertNotIn("obsolete", {link.sheet_id for link in plan.links})

    def test_unpaired_canonical_week_stops_navigation_update(self):
        sheets = [*FIXED_SHEETS, *paired_sheets(SIX_PERIODS[:5])]
        sheets.append({"sheet_id": "svc-new", "sheet_name": "慢服务0824-0830", "row_count": 10, "col_count": 26})

        with self.assertRaisesRegex(NavigationError, "非配对周期.*0824-0830"):
            build_navigation_plan(sheets)

    def test_fewer_than_five_paired_weeks_stops_navigation_update(self):
        with self.assertRaisesRegex(NavigationError, "需要 5 个完整周期"):
            build_navigation_plan([*FIXED_SHEETS, *paired_sheets(SIX_PERIODS[:4])])

    def test_fixed_sheet_id_mismatch_stops_navigation_update(self):
        sheets = [dict(sheet) for sheet in six_week_sheets()]
        next(sheet for sheet in sheets if sheet["sheet_name"] == "慢服务汇总")["sheet_id"] = "wrong"

        with self.assertRaisesRegex(NavigationError, "慢服务汇总.*ID 不匹配"):
            build_navigation_plan(sheets)


class NavigationClientDouble:
    def __init__(self, *, wrong_link=False, full_url_links=False):
        self.values = []
        self.links = {}
        self.wrong_link = wrong_link
        self.full_url_links = full_url_links

    def set_values(self, sheet_id, values):
        self.assert_directory(sheet_id)
        self.values = [dict(cell) for cell in values]

    def set_link(self, sheet_id, row, col, url, display_text):
        self.assert_directory(sheet_id)
        target = url if self.full_url_links else url.rsplit("=", 1)[-1]
        self.links[(row, col)] = {"text": display_text, "url": target}

    def get_cells(self, sheet_id, start_row, end_row, start_col, end_col):
        self.assert_directory(sheet_id)
        self.read_range = (start_row, end_row, start_col, end_col)
        cells = []
        for value in self.values:
            cell = {
                "row": value["row"],
                "col": value["col"],
                "string_value": value.get("string_value", ""),
                "hyperlinks": [],
            }
            link = self.links.get((cell["row"], cell["col"]))
            if link:
                cell["hyperlinks"] = [dict(link)]
            cells.append(cell)
        if self.wrong_link:
            target = next(cell for cell in cells if (cell["row"], cell["col"]) == (1, 13))
            target["hyperlinks"][0]["url"] = "wrong-sheet-id"
        return cells

    def assert_directory(self, sheet_id):
        if sheet_id != "BB08J2":
            raise AssertionError(sheet_id)


class DirectoryNavigationWriteTests(unittest.TestCase):
    def test_writes_only_m1_to_o8_and_verifies_raw_sheet_id_links(self):
        client = NavigationClientDouble()

        rebuild_directory_navigation(client, six_week_sheets())

        self.assertEqual(
            {(cell["row"], cell["col"]) for cell in client.values},
            {(row, col) for row in range(8) for col in range(12, 15)},
        )
        self.assertEqual(len(client.links), 14)
        self.assertEqual(client.read_range, (0, 7, 12, 14))

    def test_verifies_full_url_link_representation(self):
        client = NavigationClientDouble(full_url_links=True)

        rebuild_directory_navigation(client, six_week_sheets())

        self.assertEqual(len(client.links), 14)

    def test_wrong_link_target_fails_readback_verification(self):
        client = NavigationClientDouble(wrong_link=True)

        with self.assertRaisesRegex(NavigationError, "目录链接校验失败"):
            rebuild_directory_navigation(client, six_week_sheets())


if __name__ == "__main__":
    unittest.main()
