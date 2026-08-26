import unittest
from unittest.mock import patch

from scripts.tencent_upload_scan_reports import choose_directory_row, find_dir_row


class DirectoryRowSelectionTests(unittest.TestCase):
    def test_existing_period_reuses_its_original_row(self):
        self.assertEqual(
            choose_directory_row("0817-0823", ["", "0720-0726", "0817-0823", ""], start_row=1),
            (3, True),
        )

    def test_new_period_does_not_reuse_a_historical_hole(self):
        self.assertEqual(
            choose_directory_row("0824-0830", ["", "", "0720-0726", "", "0817-0823", ""], start_row=1),
            (6, False),
        )

    def test_all_empty_period_cells_use_first_data_row(self):
        self.assertEqual(choose_directory_row("0824-0830", ["", "", ""], start_row=1), (1, False))

    @patch("scripts.tencent_upload_scan_reports.sheetengine")
    def test_find_reads_to_physical_end_instead_of_fixed_row_twenty(self, sheetengine):
        def response(tool, arguments):
            if tool == "get_sheet_info":
                return {"sheets": [{"sheet_id": "BB08J2", "row_count": 80}]}
            if tool == "get_cell_data":
                self.assertEqual(arguments["end_row"], 79)
                return {"csv_data": "\n" * 69 + "0817-0823\n" + "\n" * 9}
            self.fail(tool)

        sheetengine.side_effect = response

        self.assertEqual(find_dir_row("0824-0830"), (71, False))


if __name__ == "__main__":
    unittest.main()
