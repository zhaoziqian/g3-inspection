import json
import tempfile
import unittest
from pathlib import Path

from scripts.backfill_scan_owners import (
    OwnerMap,
    build_owner_map_from_history,
    load_owner_map,
    plan_backfill_updates,
    save_owner_map,
)


class BackfillScanOwnersTests(unittest.TestCase):
    def test_build_owner_map_prefers_newer_history(self):
        history = [
            {
                "period": "0622-0628",
                "slow_service": {"FSSE18.query": "张三"},
                "slow_sql": {"PMPO.query": "李四"},
            },
            {
                "period": "0615-0621",
                "slow_service": {"FSSE18.query": "旧负责人", "PMPR02.query": "王五"},
                "slow_sql": {"PMPO.query": "旧SQL负责人", "SMSO.query": "赵六"},
            },
        ]

        owner_map = build_owner_map_from_history(history)

        self.assertEqual(owner_map.slow_service["FSSE18.query"], "张三")
        self.assertEqual(owner_map.slow_service["PMPR02.query"], "王五")
        self.assertEqual(owner_map.slow_sql["PMPO.query"], "李四")
        self.assertEqual(owner_map.slow_sql["SMSO.query"], "赵六")
        self.assertEqual(owner_map.metadata["sources"], ["0622-0628", "0615-0621"])

    def test_plan_backfill_only_fills_blank_owners(self):
        owner_map = OwnerMap(
            slow_service={"A.service": "张三", "B.service": "李四"},
            slow_sql={},
            metadata={},
        )
        rows = [
            {"row": 1, "name": "A.service", "owner": ""},
            {"row": 2, "name": "B.service", "owner": "已有"},
            {"row": 3, "name": "C.service", "owner": ""},
        ]

        updates, unmatched = plan_backfill_updates(rows, owner_map, "slow_service", owner_col=7)

        self.assertEqual(
            updates,
            [{"row": 1, "col": 7, "value_type": "STRING", "string_value": "张三"}],
        )
        self.assertEqual(unmatched, {"C.service"})

    def test_owner_map_json_round_trip(self):
        owner_map = OwnerMap(
            slow_service={"FSSE18.query": "张三"},
            slow_sql={"PMPO.query": "李四"},
            metadata={"updated_from_period": "0622-0628"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan_owner_map.json"

            save_owner_map(path, owner_map)
            loaded = load_owner_map(path)
            raw = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded, owner_map)
        self.assertEqual(raw["slow_service"]["FSSE18.query"], "张三")
        self.assertEqual(raw["slow_sql"]["PMPO.query"], "李四")
        self.assertEqual(raw["metadata"]["updated_from_period"], "0622-0628")


if __name__ == "__main__":
    unittest.main()
