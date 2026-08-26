#!/usr/bin/env python3
"""Archive old weekly slow-service and slow-SQL sheets in Tencent Docs."""

from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable

try:
    from scripts.scan_archive import (
        ARCHIVE_HEADERS,
        ArchiveError,
        index_archive_blocks,
        normalize_source_rows,
        plan_period_action,
        select_archive_periods,
    )
except ModuleNotFoundError:
    from scan_archive import (
        ARCHIVE_HEADERS,
        ArchiveError,
        index_archive_blocks,
        normalize_source_rows,
        plan_period_action,
        select_archive_periods,
    )


TENCENT_FILE_ID = "DWHBzb1ZFZWhFREZa"
DIRECTORY_SHEET_ID = "BB08J2"
TARGETS = {
    "slow_service": ("慢服务归档", "7i67j3"),
    "slow_sql": ("慢SQL归档", "8i29ez"),
}
SOURCE_PREFIX = {"slow_service": "慢服务", "slow_sql": "慢SQL"}
DIRECTORY_PERIOD_COL = 12


@dataclass(frozen=True)
class SheetMeta:
    sheet_id: str
    sheet_name: str
    row_count: int
    col_count: int


def _meta(raw: dict[str, Any]) -> SheetMeta:
    return SheetMeta(
        str(raw["sheet_id"]),
        str(raw["sheet_name"]),
        int(raw.get("row_count", 1)),
        int(raw.get("col_count", 1)),
    )


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def _pad_rows(rows: list[list[str]], count: int, width: int) -> list[list[str]]:
    padded = [[str(value) for value in (row + [""] * width)[:width]] for row in rows]
    padded.extend([[""] * width for _ in range(max(count - len(padded), 0))])
    return padded[:count]


def read_csv_adaptive(
    client,
    sheet_id: str,
    start_row: int,
    end_row: int,
    start_col: int,
    end_col: int,
    *,
    sheet_name: str | None = None,
) -> list[list[str]]:
    expected_rows = end_row - start_row + 1
    width = end_col - start_col + 1
    try:
        return _pad_rows(
            _parse_csv(client.get_csv(sheet_id, start_row, end_row, start_col, end_col)),
            expected_rows,
            width,
        )
    except ArchiveError as exc:
        splitworthy = "无效 JSON" in str(exc) or "截断" in str(exc)
        if not splitworthy:
            raise
        if start_row == end_row:
            label = sheet_name or sheet_id
            raise ArchiveError(f"{label} 第 {start_row + 1} 行无法完整读取: {exc}") from exc
        midpoint = (start_row + end_row) // 2
        return read_csv_adaptive(
            client, sheet_id, start_row, midpoint, start_col, end_col, sheet_name=sheet_name
        ) + read_csv_adaptive(
            client, sheet_id, midpoint + 1, end_row, start_col, end_col, sheet_name=sheet_name
        )


def _read_column(
    client,
    meta: SheetMeta,
    column: int,
    *,
    start_row: int = 1,
    batch_size: int = 150,
) -> list[str]:
    if meta.row_count <= start_row:
        return []
    values: list[str] = []
    for batch_start in range(start_row, meta.row_count, batch_size):
        batch_end = min(batch_start + batch_size - 1, meta.row_count - 1)
        rows = read_csv_adaptive(
            client,
            meta.sheet_id,
            batch_start,
            batch_end,
            column,
            column,
            sheet_name=meta.sheet_name,
        )
        values.extend(row[0] if row else "" for row in rows)
    return values


def _contiguous_groups(columns: Iterable[int]) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    for column in sorted(set(columns)):
        if groups and column == groups[-1][1] + 1:
            groups[-1] = (groups[-1][0], column)
        else:
            groups.append((column, column))
    return groups


def _read_columns(
    client,
    meta: SheetMeta,
    columns: Iterable[int],
    *,
    start_row: int = 1,
    batch_size: int = 150,
) -> dict[int, list[str]]:
    requested = sorted(set(columns))
    result = {column: [] for column in requested}
    if meta.row_count <= start_row:
        return result
    for col_start, col_end in _contiguous_groups(requested):
        for batch_start in range(start_row, meta.row_count, batch_size):
            batch_end = min(batch_start + batch_size - 1, meta.row_count - 1)
            rows = read_csv_adaptive(
                client,
                meta.sheet_id,
                batch_start,
                batch_end,
                col_start,
                col_end,
                sheet_name=meta.sheet_name,
            )
            for column in range(col_start, col_end + 1):
                result[column].extend(row[column - col_start] for row in rows)
    return result


def _read_header(client, meta: SheetMeta) -> list[str]:
    if meta.col_count <= 0:
        return []
    rows = read_csv_adaptive(client, meta.sheet_id, 0, 0, 0, meta.col_count - 1, sheet_name=meta.sheet_name)
    return [str(value).strip() for value in (rows[0] if rows else [])]


def read_source_sheet(client, meta: SheetMeta, kind: str, period: str) -> list[list[str]]:
    header = _read_header(client, meta)
    normalize_source_rows(kind, period, header, [])
    columns = {value: index for index, value in enumerate(header) if value}
    wanted = ARCHIVE_HEADERS[kind][1:]
    sql_column = columns.get("SQL语句") if kind == "slow_sql" else None
    ordinary_indexes = [index for name, index in columns.items() if name in wanted and index != sql_column]
    by_index = _read_columns(client, meta, ordinary_indexes)
    if sql_column is not None:
        by_index[sql_column] = _read_column(client, meta, sql_column, batch_size=10)
    raw_columns = {name: by_index[index] for name, index in columns.items() if name in wanted}
    row_count = max((len(values) for values in raw_columns.values()), default=0)
    raw_rows = [
        [raw_columns.get(name, [""] * row_count)[row_index] for name in wanted]
        for row_index in range(row_count)
    ]
    raw_rows = [row for row in raw_rows if any(value != "" for value in row)]
    return normalize_source_rows(kind, period, wanted, raw_rows)


def read_archive_sheet(client, meta: SheetMeta, kind: str) -> tuple[list[str], list[list[str]]]:
    header = _read_header(client, meta)
    used_header = list(ARCHIVE_HEADERS[kind])
    if not any(header):
        return [], []
    actual = (header + [""] * len(used_header))[: len(used_header)]
    if actual != used_header:
        raise ArchiveError(f"{meta.sheet_name} 表头不匹配: {actual}")
    sql_column = 5 if kind == "slow_sql" else None
    ordinary_indexes = [column for column in range(len(used_header)) if column != sql_column]
    columns = _read_columns(client, meta, ordinary_indexes)
    if sql_column is not None:
        columns[sql_column] = _read_column(client, meta, sql_column, batch_size=10)
    row_count = max((len(values) for values in columns.values()), default=0)
    rows = [[columns[column][row] for column in range(len(used_header))] for row in range(row_count)]
    while rows and not any(rows[-1]):
        rows.pop()
    if any(not any(row) for row in rows):
        raise ArchiveError(f"{meta.sheet_name} 已用区域中存在整行空洞")
    return actual, rows


def _directory_period_rows(client, meta: SheetMeta) -> dict[str, int]:
    values = _read_column(client, meta, DIRECTORY_PERIOD_COL)
    result: dict[str, int] = {}
    for offset, value in enumerate(values, start=1):
        period = str(value).strip()
        if not period:
            continue
        if period in result:
            raise ArchiveError(f"目录中周期 {period} 出现多次")
        result[period] = offset
    return result


def build_dry_run(
    client,
    *,
    keep_weeks: int = 5,
    targets: dict[str, tuple[str, str]] = TARGETS,
    directory_id: str = DIRECTORY_SHEET_ID,
) -> dict[str, Any]:
    metas = [_meta(raw) for raw in client.sheet_info()]
    by_name = {meta.sheet_name: meta for meta in metas}
    by_id = {meta.sheet_id: meta for meta in metas}
    selection = select_archive_periods(by_name, keep_weeks=keep_weeks)

    target_metas: dict[str, SheetMeta] = {}
    archive_rows: dict[str, list[list[str]]] = {}
    archive_blocks = {}
    for kind, (target_name, target_id) in targets.items():
        target = by_name.get(target_name)
        if not target or target.sheet_id != target_id:
            raise ArchiveError(f"找不到目标归档页或 sheet ID 不匹配: {target_name}")
        target_metas[kind] = target
        _header, rows = read_archive_sheet(client, target, kind)
        archive_rows[kind] = rows
        archive_blocks[kind] = index_archive_blocks(rows)

    directory = by_id.get(directory_id)
    if not directory:
        raise ArchiveError(f"找不到目录 Sheet: {directory_id}")
    directory_rows = _directory_period_rows(client, directory)

    sources: dict[str, dict[str, list[list[str]]]] = {"slow_service": {}, "slow_sql": {}}
    for period in selection.archive:
        for kind in ("slow_service", "slow_sql"):
            source_name = f"{SOURCE_PREFIX[kind]}{period}"
            source = by_name.get(source_name)
            if not source:
                raise ArchiveError(f"找不到待归档源 Sheet: {source_name}")
            sources[kind][period] = read_source_sheet(client, source, kind, period)

    report: dict[str, Any] = {
        "archive_periods": list(selection.archive),
        "keep_periods": list(selection.keep),
        "unpaired_periods": list(selection.unpaired),
        "slow_service": {},
        "slow_sql": {},
        "delete_sheets": [],
        "directory_rows": {},
    }
    for period in selection.archive:
        for kind in ("slow_service", "slow_sql"):
            existing = archive_blocks[kind].get(period)
            existing_rows = list(existing.rows) if existing else []
            source_rows = sources[kind][period]
            report[kind][period] = {
                "source_rows": len(source_rows),
                "existing_rows": len(existing_rows),
                "action": plan_period_action(source_rows, existing_rows, source_exists=True),
            }
            report["delete_sheets"].append(f"{SOURCE_PREFIX[kind]}{period}")
        if period in directory_rows:
            report["directory_rows"][period] = directory_rows[period]
    return report


class TencentArchiveClient:
    def __init__(self, file_id: str = TENCENT_FILE_ID):
        self.file_id = file_id

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = dict(arguments)
        payload["file_id"] = self.file_id
        result = subprocess.run(
            ["mcporter", "call", "sheet-mcp", tool, "--args", json.dumps(payload, ensure_ascii=False)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ArchiveError(result.stderr.strip() or result.stdout.strip())
        output = result.stdout.strip()
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            raise ArchiveError(f"sheet-mcp {tool} 返回无效 JSON: {output[:300]}") from None
        if data.get("error"):
            raise ArchiveError(str(data["error"]))
        return data

    def sheet_info(self) -> list[dict[str, Any]]:
        return self.call("get_sheet_info", {}).get("sheets", [])

    def get_csv(self, sheet_id: str, start_row: int, end_row: int, start_col: int, end_col: int) -> str:
        return self.call(
            "get_cell_data",
            {
                "sheet_id": sheet_id,
                "start_row": start_row,
                "end_row": end_row,
                "start_col": start_col,
                "end_col": end_col,
                "return_csv": True,
            },
        ).get("csv_data", "")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="归档腾讯文档中过期慢服务/慢SQL周明细。默认 dry-run。")
    parser.add_argument("--apply", action="store_true", help="实际写归档、删除旧周 Sheet 并清理目录")
    parser.add_argument("--keep-weeks", type=int, default=5, help="保留独立周 Sheet 的最近周数（默认 5）")
    parser.add_argument("--file-id", default=TENCENT_FILE_ID)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        report = build_dry_run(TencentArchiveClient(args.file_id), keep_weeks=args.keep_weeks)
        if args.apply:
            raise ArchiveError("apply 尚未实现")
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
