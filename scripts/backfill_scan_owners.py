#!/usr/bin/env python3
"""Maintain and use slow-service / slow-SQL owner mappings."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OWNER_MAP = SKILL_DIR / "references" / "scan_owner_map.json"
TENCENT_FILE_ID = "DWHBzb1ZFZWhFREZa"

SHEET_TYPES = {
    "slow_service": {"prefix": "慢服务", "name_header": "服务名"},
    "slow_sql": {"prefix": "慢SQL", "name_header": "SQL名"},
}


@dataclass(eq=True)
class OwnerMap:
    slow_service: dict[str, str] = field(default_factory=dict)
    slow_sql: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


def load_owner_map(path: Path) -> OwnerMap:
    data = json.loads(path.read_text(encoding="utf-8"))
    return OwnerMap(
        slow_service=dict(data.get("slow_service", {})),
        slow_sql=dict(data.get("slow_sql", {})),
        metadata=dict(data.get("metadata", {})),
    )


def save_owner_map(path: Path, owner_map: OwnerMap) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "slow_service": dict(sorted(owner_map.slow_service.items())),
        "slow_sql": dict(sorted(owner_map.slow_sql.items())),
        "metadata": owner_map.metadata,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_owner_map_from_history(history: list[dict[str, Any]]) -> OwnerMap:
    owner_map = OwnerMap(metadata={"sources": []})
    for source in history:
        period = source.get("period")
        if period:
            owner_map.metadata["sources"].append(period)
        for key in ("slow_service", "slow_sql"):
            target = getattr(owner_map, key)
            for name, owner in source.get(key, {}).items():
                if name and owner and name not in target:
                    target[name] = owner
    return owner_map


def merge_owner_map(base: OwnerMap, update: OwnerMap, source_period: str) -> tuple[int, int]:
    changed = 0
    added = 0
    for key in ("slow_service", "slow_sql"):
        target = getattr(base, key)
        incoming = getattr(update, key)
        for name, owner in incoming.items():
            old = target.get(name)
            if old == owner:
                continue
            if old is None:
                added += 1
            changed += 1
            target[name] = owner
    base.metadata["updated_from_period"] = source_period
    return added, changed


def plan_backfill_updates(
    rows: list[dict[str, str | int]],
    owner_map: OwnerMap,
    map_key: str,
    owner_col: int,
) -> tuple[list[dict[str, Any]], set[str]]:
    mapping = getattr(owner_map, map_key)
    updates: list[dict[str, Any]] = []
    unmatched: set[str] = set()
    for row in rows:
        name = str(row.get("name", "")).strip()
        owner = str(row.get("owner", "")).strip()
        if not name or owner:
            continue
        mapped_owner = mapping.get(name)
        if mapped_owner:
            updates.append(
                {
                    "row": int(row["row"]),
                    "col": owner_col,
                    "value_type": "STRING",
                    "string_value": mapped_owner,
                }
            )
        else:
            unmatched.add(name)
    return updates, unmatched


class TencentSheetClient:
    def __init__(self, file_id: str = TENCENT_FILE_ID):
        self.file_id = file_id

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(args, ensure_ascii=False)
        result = subprocess.run(
            ["mcporter", "call", "tencent-sheetengine", tool, "--args", payload],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        data = json.loads(result.stdout)
        if data.get("error"):
            raise RuntimeError(data["error"])
        return data

    def sheet_info(self) -> list[dict[str, Any]]:
        return self.call("get_sheet_info", {"file_id": self.file_id})["sheets"]

    def cells(self, sheet_id: str, start_row: int, end_row: int, start_col: int, end_col: int) -> list[dict[str, Any]]:
        data = self.call(
            "get_cell_data",
            {
                "file_id": self.file_id,
                "sheet_id": sheet_id,
                "start_row": start_row,
                "end_row": end_row,
                "start_col": start_col,
                "end_col": end_col,
                "return_csv": False,
            },
        )
        return data.get("cells", [])

    def set_values(self, sheet_id: str, values: list[dict[str, Any]]) -> None:
        self.call("set_range_value", {"file_id": self.file_id, "sheet_id": sheet_id, "values": values})


def cell_text(cell: dict[str, Any]) -> str:
    value_type = cell.get("value_type")
    if value_type == "STRING":
        return (cell.get("string_value") or "").strip()
    if value_type == "NUMBER":
        return str(cell.get("number_value", "")).strip()
    if value_type == "BOOL":
        return "TRUE" if cell.get("bool_value") else "FALSE"
    return (cell.get("string_value") or "").strip()


def sheet_period(sheet_name: str, prefix: str) -> str | None:
    match = re.fullmatch(rf"{re.escape(prefix)}(\d{{4}}-\d{{4}})", sheet_name)
    return match.group(1) if match else None


def header_columns(client: TencentSheetClient, sheet_id: str) -> dict[str, int]:
    return {
        cell_text(cell): cell["col"]
        for cell in client.cells(sheet_id, 0, 0, 0, 25)
        if cell_text(cell)
    }


def column_values(client: TencentSheetClient, sheet_id: str, row_count: int, col: int, chunk_rows: int = 80) -> dict[int, str]:
    values: dict[int, str] = {}
    for start in range(0, row_count, chunk_rows):
        end = min(row_count - 1, start + chunk_rows - 1)
        for cell in client.cells(sheet_id, start, end, col, col):
            values[cell["row"]] = cell_text(cell)
    return values


def read_sheet_owner_pairs(client: TencentSheetClient, sheet: dict[str, Any], map_key: str) -> dict[str, str]:
    config = SHEET_TYPES[map_key]
    headers = header_columns(client, sheet["sheet_id"])
    name_col = headers.get(config["name_header"])
    owner_col = headers.get("责任人")
    if name_col is None or owner_col is None:
        raise RuntimeError(f"{sheet['sheet_name']} 缺少 {config['name_header']} 或 责任人列")
    names = column_values(client, sheet["sheet_id"], sheet["row_count"], name_col)
    owners = column_values(client, sheet["sheet_id"], sheet["row_count"], owner_col)
    pairs: dict[str, str] = {}
    for row in range(1, sheet["row_count"]):
        name = names.get(row, "").strip()
        owner = owners.get(row, "").strip()
        if name and owner and name not in pairs:
            pairs[name] = owner
    return pairs


def read_current_rows(
    client: TencentSheetClient,
    sheet: dict[str, Any],
    map_key: str,
) -> tuple[list[dict[str, str | int]], int]:
    config = SHEET_TYPES[map_key]
    headers = header_columns(client, sheet["sheet_id"])
    name_col = headers.get(config["name_header"])
    owner_col = headers.get("责任人")
    if name_col is None or owner_col is None:
        raise RuntimeError(f"{sheet['sheet_name']} 缺少 {config['name_header']} 或 责任人列")
    names = column_values(client, sheet["sheet_id"], sheet["row_count"], name_col)
    owners = column_values(client, sheet["sheet_id"], sheet["row_count"], owner_col)
    rows = [
        {"row": row, "name": names.get(row, ""), "owner": owners.get(row, "")}
        for row in range(1, sheet["row_count"])
        if names.get(row, "")
    ]
    return rows, owner_col


def sheets_by_type_and_period(sheets: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    indexed: dict[str, dict[str, dict[str, Any]]] = {"slow_service": {}, "slow_sql": {}}
    for sheet in sheets:
        for map_key, config in SHEET_TYPES.items():
            period = sheet_period(sheet["sheet_name"], config["prefix"])
            if period:
                indexed[map_key][period] = sheet
    return indexed


def build_history_from_sheets(
    client: TencentSheetClient,
    periods: list[str],
    indexed: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    history = []
    for period in periods:
        item: dict[str, Any] = {"period": period, "slow_service": {}, "slow_sql": {}}
        for map_key in ("slow_service", "slow_sql"):
            sheet = indexed[map_key].get(period)
            if sheet:
                item[map_key] = read_sheet_owner_pairs(client, sheet, map_key)
        history.append(item)
    return history


def immediate_previous_period(period: str, periods: list[str]) -> str:
    previous = [item for item in periods if item < period]
    if not previous:
        raise RuntimeError(f"找不到 {period} 之前的历史周期")
    return max(previous)


def batch_write_updates(client: TencentSheetClient, sheet_id: str, updates: list[dict[str, Any]], dry_run: bool) -> None:
    if dry_run:
        return
    for start in range(0, len(updates), 100):
        client.set_values(sheet_id, updates[start : start + 100])


def run(args: argparse.Namespace) -> int:
    client = TencentSheetClient(args.file_id)
    sheets = client.sheet_info()
    indexed = sheets_by_type_and_period(sheets)
    all_periods = sorted(set(indexed["slow_service"]) | set(indexed["slow_sql"]), reverse=True)
    map_path = Path(args.owner_map)

    if args.init_config:
        history = build_history_from_sheets(client, all_periods, indexed)
        owner_map = build_owner_map_from_history(history)
        owner_map.metadata["initialized_from_periods"] = all_periods
        save_owner_map(map_path, owner_map)
        print(f"已初始化责任人配置: {map_path}")
        print(f"慢服务映射: {len(owner_map.slow_service)}，慢SQL映射: {len(owner_map.slow_sql)}")
        if not (args.update_from_previous or args.backfill):
            return 0
    else:
        owner_map = load_owner_map(map_path) if map_path.exists() else OwnerMap()

    if args.update_from_previous:
        if not args.period:
            raise RuntimeError("--update-from-previous 需要 --period MMDD-MMDD")
        source_period = immediate_previous_period(args.period, sorted(all_periods))
        history = build_history_from_sheets(client, [source_period], indexed)
        update_map = build_owner_map_from_history(history)
        added, changed = merge_owner_map(owner_map, update_map, source_period)
        save_owner_map(map_path, owner_map)
        print(f"已用最近周期 {source_period} 更新责任人配置: 新增 {added}，变更 {changed}")

    if args.backfill:
        if not args.period:
            raise RuntimeError("--backfill 需要 --period MMDD-MMDD")
        for map_key, config in SHEET_TYPES.items():
            sheet = indexed[map_key].get(args.period)
            if not sheet:
                raise RuntimeError(f"找不到 {config['prefix']}{args.period}")
            rows, owner_col = read_current_rows(client, sheet, map_key)
            updates, unmatched = plan_backfill_updates(rows, owner_map, map_key, owner_col)
            batch_write_updates(client, sheet["sheet_id"], updates, args.dry_run)
            blank_before = sum(1 for row in rows if not str(row.get("owner", "")).strip())
            print(
                f"{sheet['sheet_name']}: 数据行 {len(rows)}，空白 {blank_before}，"
                f"回填 {len(updates)}，未匹配唯一项 {len(unmatched)}"
            )
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="维护慢服务/慢SQL责任人配置并回填当前周责任人。")
    parser.add_argument("--period", help="当前周期，格式 MMDD-MMDD，例如 0629-0705")
    parser.add_argument("--init-config", action="store_true", help="从历史 sheet 初始化责任人配置")
    parser.add_argument("--update-from-previous", action="store_true", help="用当前周期的上一周期更新责任人配置")
    parser.add_argument("--backfill", action="store_true", help="根据责任人配置回填当前周期空白责任人")
    parser.add_argument("--dry-run", action="store_true", help="只计算，不写回腾讯文档")
    parser.add_argument("--owner-map", default=str(DEFAULT_OWNER_MAP), help="责任人配置 JSON 路径")
    parser.add_argument("--file-id", default=TENCENT_FILE_ID, help="腾讯文档 file_id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not (args.init_config or args.update_from_previous or args.backfill):
        raise SystemExit("至少指定 --init-config、--update-from-previous 或 --backfill 之一")
    try:
        return run(args)
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
