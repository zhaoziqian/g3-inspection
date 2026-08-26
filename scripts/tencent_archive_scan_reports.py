#!/usr/bin/env python3
"""Archive old weekly slow-service and slow-SQL sheets in Tencent Docs."""

from __future__ import annotations

import argparse
import base64
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
        row_digest,
        select_archive_periods,
    )
except ModuleNotFoundError:
    from scan_archive import (
        ARCHIVE_HEADERS,
        ArchiveError,
        index_archive_blocks,
        normalize_source_rows,
        plan_period_action,
        row_digest,
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


@dataclass(frozen=True)
class ArchivePreparation:
    report: dict[str, Any]
    periods: tuple[str, ...]
    target_metas: dict[str, SheetMeta]
    current_rows: dict[str, list[list[str]]]
    sources: dict[str, dict[str, list[list[str]]]]
    source_metas: dict[str, dict[str, SheetMeta]]
    directory_meta: SheetMeta
    directory_rows: dict[str, int]
    directory_sentinels: dict[str, tuple[tuple[str, ...], tuple[str, ...]]]


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
        by_index[sql_column] = _read_column(client, meta, sql_column, batch_size=50)
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
        columns[sql_column] = _read_column(client, meta, sql_column, batch_size=50)
    row_count = max((len(values) for values in columns.values()), default=0)
    rows = [[columns[column][row] for column in range(len(used_header))] for row in range(row_count)]
    while rows and not any(rows[-1]):
        rows.pop()
    if any(not any(row) for row in rows):
        raise ArchiveError(f"{meta.sheet_name} 已用区域中存在整行空洞")
    return actual, rows


def make_row_cells(kind: str, row_index: int, row: Iterable[str]) -> list[dict[str, Any]]:
    values = [str(value) for value in row]
    width = len(ARCHIVE_HEADERS[kind])
    values = (values + [""] * width)[:width]
    cells: list[dict[str, Any]] = []
    for column, value in enumerate(values):
        cell: dict[str, Any] = {"row": row_index, "col": column, "value_type": "STRING"}
        if kind == "slow_sql" and column == 5 and value:
            cell["value_base64"] = base64.b64encode(value.encode("utf-8")).decode("ascii")
        else:
            cell["string_value"] = value
        cells.append(cell)
    return cells


def _write_cell_batches(client, sheet_id: str, cells: Iterable[dict[str, Any]], max_bytes: int = 500_000) -> None:
    batch: list[dict[str, Any]] = []
    size = 0
    for cell in cells:
        cell_size = len(json.dumps(cell, ensure_ascii=False).encode("utf-8")) + 1
        if batch and size + cell_size > max_bytes:
            client.set_values(sheet_id, batch)
            batch = []
            size = 0
        batch.append(cell)
        size += cell_size
    if batch:
        client.set_values(sheet_id, batch)


def _desired_archive_rows(
    current_rows: Iterable[Iterable[str]],
    source_by_period: dict[str, list[list[str]]],
) -> list[list[str]]:
    desired = [[str(value) for value in row] for row in current_rows]
    for period, source_rows in source_by_period.items():
        blocks = index_archive_blocks(desired)
        block = blocks.get(period)
        if block:
            desired[block.start : block.end] = [list(row) for row in source_rows]
        else:
            desired.extend([list(row) for row in source_rows])
    index_archive_blocks(desired)
    return desired


def apply_archive_sheet(
    client,
    meta: SheetMeta,
    kind: str,
    current_rows: list[list[str]],
    source_by_period: dict[str, list[list[str]]],
) -> list[list[str]]:
    desired = _desired_archive_rows(current_rows, source_by_period)
    needed_rows = 1 + len(desired)
    if needed_rows > meta.row_count:
        client.insert_dimension(
            meta.sheet_id,
            dimension_type="row",
            index=max(meta.row_count - 1, 0),
            count=needed_rows - meta.row_count,
            direction="after",
        )

    if not current_rows:
        _write_cell_batches(client, meta.sheet_id, make_row_cells(kind, 0, ARCHIVE_HEADERS[kind]))

    working = [list(row) for row in current_rows]
    for period, source_rows in source_by_period.items():
        blocks = index_archive_blocks(working)
        block = blocks.get(period)
        existing = list(block.rows) if block else []
        action = plan_period_action(source_rows, existing, source_exists=True)
        if action == "skip":
            continue
        if action == "append":
            start = len(working)
            working.extend([list(row) for row in source_rows])
        else:
            assert block is not None
            start = block.start
            old_count = block.end - block.start
            new_count = len(source_rows)
            absolute_start = start + 1
            if old_count != new_count:
                client.delete_dimension(
                    meta.sheet_id,
                    dimension_type="row",
                    index=absolute_start,
                    count=old_count,
                )
                if new_count:
                    client.insert_dimension(
                        meta.sheet_id,
                        dimension_type="row",
                        index=absolute_start,
                        count=new_count,
                        direction="before",
                    )
            else:
                client.clear_range(
                    meta.sheet_id,
                    start_row=absolute_start,
                    end_row=absolute_start + old_count - 1,
                    start_col=0,
                    end_col=len(ARCHIVE_HEADERS[kind]) - 1,
                )
            working[start : block.end] = [list(row) for row in source_rows]
        cells = [
            cell
            for offset, row in enumerate(source_rows)
            for cell in make_row_cells(kind, start + 1 + offset, row)
        ]
        _write_cell_batches(client, meta.sheet_id, cells)

    if working != desired:
        raise ArchiveError(f"{meta.sheet_name} 内部写入计划不一致")
    client.style_archive(meta.sheet_id, len(desired), len(ARCHIVE_HEADERS[kind]) - 1)
    return desired


def verify_archive_pair(
    client,
    target_metas: dict[str, SheetMeta],
    expected_by_kind: dict[str, list[list[str]]],
) -> None:
    for kind in ("slow_service", "slow_sql"):
        expected = expected_by_kind[kind]
        header, actual = read_archive_sheet(client, target_metas[kind], kind)
        if header != ARCHIVE_HEADERS[kind]:
            raise ArchiveError(f"{target_metas[kind].sheet_name} 表头写后校验失败")
        if len(actual) != len(expected):
            raise ArchiveError(
                f"{target_metas[kind].sheet_name} 行数不一致: 期望 {len(expected)}，实际 {len(actual)}"
            )
        actual_hashes = [row_digest(row) for row in actual]
        expected_hashes = [row_digest(row) for row in expected]
        if actual_hashes != expected_hashes:
            mismatch = next(
                (index for index, pair in enumerate(zip(actual_hashes, expected_hashes)) if pair[0] != pair[1]),
                0,
            )
            raise ArchiveError(f"{target_metas[kind].sheet_name} 第 {mismatch + 2} 行内容不一致")
        index_archive_blocks(actual)


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


def read_directory_sentinels(
    client,
    meta: SheetMeta,
    period_rows: dict[str, int],
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    if not period_rows:
        return {}
    first_row = min(period_rows.values())
    last_row = max(period_rows.values())
    left = read_csv_adaptive(client, meta.sheet_id, first_row, last_row, 0, 11, sheet_name=meta.sheet_name)
    right = read_csv_adaptive(client, meta.sheet_id, first_row, last_row, 15, 25, sheet_name=meta.sheet_name)
    return {
        period: (
            tuple(left[row_index - first_row]),
            tuple(right[row_index - first_row]),
        )
        for period, row_index in period_rows.items()
    }


def clear_directory_periods(
    client,
    meta: SheetMeta,
    period_rows: dict[str, int],
    snapshots: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
) -> None:
    for period, row_index in period_rows.items():
        if period not in snapshots:
            raise ArchiveError(f"目录周期 {period} 缺少清理前快照")
        client.clear_link(meta.sheet_id, row_index, 13)
        client.clear_link(meta.sheet_id, row_index, 14)
        client.clear_range(
            meta.sheet_id,
            start_row=row_index,
            end_row=row_index,
            start_col=12,
            end_col=14,
        )
    if not period_rows:
        return
    first_row = min(period_rows.values())
    last_row = max(period_rows.values())
    cleared = read_csv_adaptive(client, meta.sheet_id, first_row, last_row, 12, 14, sheet_name=meta.sheet_name)
    for period, row_index in period_rows.items():
        if any(cleared[row_index - first_row]):
            raise ArchiveError(f"目录周期 {period} 的 M:O 未清空")
    after = read_directory_sentinels(client, meta, period_rows)
    for period, before in snapshots.items():
        if after.get(period) != before:
            raise ArchiveError(f"目录周期 {period} 同行 A:L 或 P:Z 发生变化")


def delete_verified_sources(
    client,
    source_metas: dict[str, dict[str, SheetMeta]],
    periods: Iterable[str],
    *,
    archives_verified: bool,
) -> list[str]:
    if not archives_verified:
        raise ArchiveError("尚未通过双归档校验，禁止删除源 Sheet")
    deleted: list[str] = []
    deleted_ids: list[str] = []
    for period in periods:
        for kind in ("slow_service", "slow_sql"):
            meta = source_metas.get(kind, {}).get(period)
            if not meta:
                continue
            expected_name = f"{SOURCE_PREFIX[kind]}{period}"
            if meta.sheet_name != expected_name:
                raise ArchiveError(f"拒绝删除非规范 Sheet: {meta.sheet_name}")
            client.delete_sheet(meta.sheet_id)
            deleted.append(meta.sheet_name)
            deleted_ids.append(meta.sheet_id)
    remaining = {str(sheet["sheet_id"]): str(sheet["sheet_name"]) for sheet in client.sheet_info()}
    still_present = [remaining[sheet_id] for sheet_id in deleted_ids if sheet_id in remaining]
    if still_present:
        raise ArchiveError(f"源 Sheet 删除后仍存在: {', '.join(still_present)}")
    return deleted


def prepare_archive(
    client,
    *,
    keep_weeks: int = 5,
    targets: dict[str, tuple[str, str]] = TARGETS,
    directory_id: str = DIRECTORY_SHEET_ID,
) -> ArchivePreparation:
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

    base_periods = list(selection.archive)
    archived_in_both = set(archive_blocks["slow_service"]) & set(archive_blocks["slow_sql"])
    recovery_periods = [
        period
        for period in archive_blocks["slow_service"]
        if period in archived_in_both
        and period in directory_rows
        and period not in base_periods
        and (
            f"慢服务{period}" not in by_name
            or f"慢SQL{period}" not in by_name
        )
    ]
    periods = tuple(base_periods + recovery_periods)

    sources: dict[str, dict[str, list[list[str]]]] = {"slow_service": {}, "slow_sql": {}}
    source_metas: dict[str, dict[str, SheetMeta]] = {"slow_service": {}, "slow_sql": {}}
    source_exists: dict[str, dict[str, bool]] = {"slow_service": {}, "slow_sql": {}}
    for period in periods:
        for kind in ("slow_service", "slow_sql"):
            source_name = f"{SOURCE_PREFIX[kind]}{period}"
            source = by_name.get(source_name)
            source_exists[kind][period] = source is not None
            if source:
                source_metas[kind][period] = source
                sources[kind][period] = read_source_sheet(client, source, kind, period)
            else:
                existing = archive_blocks[kind].get(period)
                if not existing:
                    raise ArchiveError(f"找不到待归档源 Sheet 且归档块不存在: {source_name}")
                sources[kind][period] = [list(row) for row in existing.rows]

    report: dict[str, Any] = {
        "archive_periods": list(periods),
        "recovery_periods": recovery_periods,
        "keep_periods": list(selection.keep),
        "unpaired_periods": list(selection.unpaired),
        "slow_service": {},
        "slow_sql": {},
        "delete_sheets": [],
        "directory_rows": {},
    }
    for period in periods:
        for kind in ("slow_service", "slow_sql"):
            existing = archive_blocks[kind].get(period)
            existing_rows = list(existing.rows) if existing else []
            source_rows = sources[kind][period]
            report[kind][period] = {
                "source_rows": len(source_rows),
                "existing_rows": len(existing_rows),
                "action": plan_period_action(
                    source_rows,
                    existing_rows,
                    source_exists=source_exists[kind][period],
                ),
            }
            if source_exists[kind][period]:
                report["delete_sheets"].append(f"{SOURCE_PREFIX[kind]}{period}")
        if period in directory_rows:
            report["directory_rows"][period] = directory_rows[period]
    selected_directory_rows = {
        period: directory_rows[period]
        for period in periods
        if period in directory_rows
    }
    sentinels = read_directory_sentinels(client, directory, selected_directory_rows)
    return ArchivePreparation(
        report=report,
        periods=periods,
        target_metas=target_metas,
        current_rows=archive_rows,
        sources=sources,
        source_metas=source_metas,
        directory_meta=directory,
        directory_rows=selected_directory_rows,
        directory_sentinels=sentinels,
    )


def build_dry_run(
    client,
    *,
    keep_weeks: int = 5,
    targets: dict[str, tuple[str, str]] = TARGETS,
    directory_id: str = DIRECTORY_SHEET_ID,
) -> dict[str, Any]:
    return prepare_archive(
        client,
        keep_weeks=keep_weeks,
        targets=targets,
        directory_id=directory_id,
    ).report


def run_archive(
    client,
    *,
    keep_weeks: int = 5,
    apply: bool = False,
    targets: dict[str, tuple[str, str]] = TARGETS,
    directory_id: str = DIRECTORY_SHEET_ID,
) -> dict[str, Any]:
    preparation = prepare_archive(
        client,
        keep_weeks=keep_weeks,
        targets=targets,
        directory_id=directory_id,
    )
    report = preparation.report
    if not apply or not preparation.periods:
        return report

    expected: dict[str, list[list[str]]] = {}
    for kind in ("slow_service", "slow_sql"):
        expected[kind] = apply_archive_sheet(
            client,
            preparation.target_metas[kind],
            kind,
            preparation.current_rows[kind],
            preparation.sources[kind],
        )

    refreshed = {_meta(raw).sheet_id: _meta(raw) for raw in client.sheet_info()}
    target_metas = {
        kind: refreshed.get(meta.sheet_id, meta)
        for kind, meta in preparation.target_metas.items()
    }
    verify_archive_pair(client, target_metas, expected)
    report["archives_verified"] = True

    report["deleted_sheets"] = delete_verified_sources(
        client,
        preparation.source_metas,
        preparation.periods,
        archives_verified=True,
    )
    clear_directory_periods(
        client,
        preparation.directory_meta,
        preparation.directory_rows,
        preparation.directory_sentinels,
    )
    report["directory_cleared"] = list(preparation.directory_rows)
    return report


class TencentArchiveClient:
    def __init__(self, file_id: str = TENCENT_FILE_ID):
        self.file_id = file_id

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = dict(arguments)
        payload["file_id"] = self.file_id
        result = subprocess.run(
            [
                "mcporter", "call", "sheet-mcp", tool,
                "--output", "json",
                "--args", json.dumps(payload, ensure_ascii=False),
            ],
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

    def insert_dimension(self, sheet_id: str, **arguments) -> None:
        self.call("insert_dimension", {"sheet_id": sheet_id, **arguments})

    def delete_dimension(self, sheet_id: str, **arguments) -> None:
        self.call("delete_dimension", {"sheet_id": sheet_id, **arguments})

    def clear_range(self, sheet_id: str, **arguments) -> None:
        self.call("clear_range_cells", {"sheet_id": sheet_id, **arguments})

    def clear_link(self, sheet_id: str, row: int, col: int) -> None:
        self.call("clear_link", {"sheet_id": sheet_id, "row": row, "col": col})

    def delete_sheet(self, sheet_id: str) -> None:
        self.call("delete_sheet", {"sheet_id": sheet_id})

    def set_values(self, sheet_id: str, values: list[dict[str, Any]]) -> None:
        self.call("set_range_value", {"sheet_id": sheet_id, "values": values})

    def style_archive(self, sheet_id: str, end_row: int, end_col: int) -> None:
        self.call(
            "set_cell_style",
            {
                "sheet_id": sheet_id,
                "start_row": 0,
                "end_row": 0,
                "start_col": 0,
                "end_col": end_col,
                "bg_color": "FF4472C4",
                "font_color": "FFFFFFFF",
                "bold": True,
                "horizontal_align": "center",
                "vertical_align": "center",
                "wrap_text": True,
            },
        )
        if end_row >= 1:
            self.call(
                "set_cell_style",
                {
                    "sheet_id": sheet_id,
                    "start_row": 1,
                    "end_row": end_row,
                    "start_col": 0,
                    "end_col": end_col,
                    "vertical_align": "top",
                    "wrap_text": True,
                },
            )
        self.call("set_freeze", {"sheet_id": sheet_id, "row_count": 1, "col_count": 0})
        self.call("remove_filter", {"sheet_id": sheet_id})
        self.call(
            "set_filter",
            {
                "sheet_id": sheet_id,
                "filter_id": f"g3_archive_{sheet_id}",
                "start_row": 0,
                "end_row": end_row,
                "start_col": 0,
                "end_col": end_col,
            },
        )
        widths = [105, 145, 145, 140, 280, 400, 110, 300, 100, 100, 300, 200]
        self.call(
            "set_dimension_size",
            {
                "sheet_id": sheet_id,
                "dimensions": [
                    {"dimension_type": "col", "index": index, "size": width}
                    for index, width in enumerate(widths[: end_col + 1])
                ],
            },
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="归档腾讯文档中过期慢服务/慢SQL周明细。默认 dry-run。")
    parser.add_argument("--apply", action="store_true", help="实际写归档、删除旧周 Sheet 并清理目录")
    parser.add_argument("--keep-weeks", type=int, default=5, help="保留独立周 Sheet 的最近周数（默认 5）")
    parser.add_argument("--file-id", default=TENCENT_FILE_ID)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        report = run_archive(
            TencentArchiveClient(args.file_id),
            keep_weeks=args.keep_weeks,
            apply=args.apply,
        )
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
