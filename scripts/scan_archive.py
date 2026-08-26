"""Pure planning helpers for weekly slow-service and slow-SQL archives."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence


class ArchiveError(RuntimeError):
    """Raised when archive state is unsafe or inconsistent."""


ARCHIVE_HEADERS = {
    "slow_service": [
        "周期", "时间窗口开始", "时间窗口结束", "应用英文名", "服务名",
        "平均耗时(ms)", "调用次数", "链路详情", "责任人", "处置状态", "处置方案", "备注（原因）",
    ],
    "slow_sql": [
        "周期", "时间窗口开始", "时间窗口结束", "应用英文名", "SQL名",
        "SQL语句", "耗时(ms)", "链路详情", "责任人", "处置状态", "处置方案", "备注（原因）",
    ],
}
OPTIONAL_SOURCE_HEADERS = {"链路详情"}


@dataclass(frozen=True)
class PeriodSelection:
    archive: tuple[str, ...]
    keep: tuple[str, ...]
    unpaired: tuple[str, ...]


@dataclass(frozen=True)
class ArchiveBlock:
    period: str
    start: int
    end: int
    rows: tuple[tuple[str, ...], ...]


_SHEET_PATTERN = re.compile(r"^(慢服务|慢SQL)(\d{4}-\d{4})$")


def _validate_mmdd(value: str) -> None:
    try:
        date(2000, int(value[:2]), int(value[2:]))
    except (ValueError, TypeError) as exc:
        raise ArchiveError(f"非法周期日期: {value}") from exc


def _validate_period(period: str) -> None:
    if not re.fullmatch(r"\d{4}-\d{4}", period):
        raise ArchiveError(f"非法周期格式: {period}")
    _validate_mmdd(period[:4])
    _validate_mmdd(period[5:])


def parse_week_sheet_name(name: str) -> tuple[str, str] | None:
    match = _SHEET_PATTERN.fullmatch(str(name))
    if not match:
        return None
    period = match.group(2)
    _validate_period(period)
    kind = "slow_service" if match.group(1) == "慢服务" else "slow_sql"
    return kind, period


def _period_order(periods: Iterable[str]) -> list[str]:
    unique = sorted(set(periods), key=lambda item: int(item[:4]))
    starts = [int(period[:4]) for period in unique]
    crosses_year = any(start >= 1101 for start in starts) and any(start <= 229 for start in starts)
    if crosses_year:
        return sorted(unique, key=lambda item: int(item[:4]) + (10000 if int(item[:4]) <= 229 else 0))
    return unique


def select_archive_periods(sheet_names: Iterable[str], keep_weeks: int = 5) -> PeriodSelection:
    if keep_weeks < 0:
        raise ArchiveError("keep_weeks 不能小于 0")
    by_kind = {"slow_service": set(), "slow_sql": set()}
    for name in sheet_names:
        parsed = parse_week_sheet_name(name)
        if parsed:
            kind, period = parsed
            by_kind[kind].add(period)
    paired = by_kind["slow_service"] & by_kind["slow_sql"]
    ordered = _period_order(paired)
    split = max(len(ordered) - keep_weeks, 0)
    unpaired = _period_order(by_kind["slow_service"] ^ by_kind["slow_sql"])
    return PeriodSelection(tuple(ordered[:split]), tuple(ordered[split:]), tuple(unpaired))


def normalize_source_rows(
    kind: str,
    period: str,
    header: Sequence[str],
    rows: Iterable[Sequence[str]],
) -> list[list[str]]:
    if kind not in ARCHIVE_HEADERS:
        raise ArchiveError(f"未知归档类型: {kind}")
    _validate_period(period)
    columns = {str(value).strip(): index for index, value in enumerate(header) if str(value).strip()}
    source_headers = ARCHIVE_HEADERS[kind][1:]
    missing = [name for name in source_headers if name not in columns and name not in OPTIONAL_SOURCE_HEADERS]
    if missing:
        raise ArchiveError(f"源 Sheet 缺少必需表头: {', '.join(missing)}")
    normalized: list[list[str]] = []
    for source_row in rows:
        values = list(source_row)
        result = [period]
        for name in source_headers:
            index = columns.get(name)
            result.append(str(values[index]) if index is not None and index < len(values) else "")
        normalized.append(result)
    return normalized


def row_digest(row: Sequence[str]) -> str:
    payload = json.dumps([str(value) for value in row], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def index_archive_blocks(rows: Iterable[Sequence[str]]) -> dict[str, ArchiveBlock]:
    materialized = [tuple(str(value) for value in row) for row in rows]
    blocks: dict[str, ArchiveBlock] = {}
    start = 0
    while start < len(materialized):
        if not materialized[start] or not materialized[start][0]:
            raise ArchiveError(f"归档第 {start + 2} 行缺少周期")
        period = materialized[start][0]
        _validate_period(period)
        end = start + 1
        while end < len(materialized) and materialized[end] and materialized[end][0] == period:
            end += 1
        if period in blocks:
            raise ArchiveError(f"周期 {period} 在归档页中不连续")
        blocks[period] = ArchiveBlock(period, start, end, tuple(materialized[start:end]))
        start = end
    if list(blocks) != _period_order(blocks):
        raise ArchiveError("归档周期顺序不是从旧到新")
    return blocks


def plan_period_action(
    source_rows: Iterable[Sequence[str]],
    existing_rows: Iterable[Sequence[str]],
    *,
    source_exists: bool,
) -> str:
    source_hashes = [row_digest(row) for row in source_rows]
    existing_hashes = [row_digest(row) for row in existing_rows]
    if source_hashes == existing_hashes:
        return "skip"
    if not source_exists:
        raise ArchiveError("源 Sheet 已不存在，无法验证或修复归档内容")
    return "replace" if existing_hashes else "append"
