#!/usr/bin/env python3
"""Pure aggregation and planning logic for weekly scan summary updates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation


FIXED_HEADERS = {
    "slow_service": ["应用英文名", "服务名", "责任人", "处置状态", "处置方案", "备注"],
    "slow_sql": ["应用英文名", "SQL名", "责任人", "处置状态", "处置方案", "备注"],
}


class SummaryUpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class FixedFields:
    owner: str = ""
    status: str = ""
    solution: str = ""
    remark: str = ""


@dataclass(frozen=True)
class CurrentItem:
    key: tuple[str, str]
    fixed: FixedFields
    value: str


@dataclass
class SummaryUpdatePlan:
    kind: str
    period: str
    insert_period: bool
    period_col: int
    value_updates: dict[tuple[int, int], str] = field(default_factory=dict)
    append_rows: list[list[str]] = field(default_factory=list)


def _decimal(value: object, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        raise SummaryUpdateError(f"{field_name} 不是有效数字: {value!r}") from None
    if not parsed.is_finite():
        raise SummaryUpdateError(f"{field_name} 不是有限数: {value!r}")
    return parsed


def _format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _latest_nonempty(rows: list[tuple[int, dict[str, str]]], header: str) -> str:
    ordered = sorted(
        rows,
        key=lambda item: (
            _parse_time(item[1].get("时间窗口结束")) is not None,
            _parse_time(item[1].get("时间窗口结束")) or datetime.min,
            item[0],
        ),
        reverse=True,
    )
    for _row_number, row in ordered:
        value = str(row.get(header, "") or "").strip()
        if value:
            return value
    return ""


def aggregate_current_week(kind: str, rows: list[dict[str, str]]) -> dict[tuple[str, str], CurrentItem]:
    if kind not in FIXED_HEADERS:
        raise ValueError(f"不支持的汇总类型: {kind}")
    item_header = "服务名" if kind == "slow_service" else "SQL名"
    latency_header = "平均耗时(ms)" if kind == "slow_service" else "耗时(ms)"
    grouped: dict[tuple[str, str], list[tuple[int, dict[str, str]]]] = {}

    for row_number, row in enumerate(rows, start=2):
        key = (
            str(row.get("应用英文名", "") or "").strip(),
            str(row.get(item_header, "") or "").strip(),
        )
        if not all(key):
            raise SummaryUpdateError(f"第 {row_number} 行应用英文名或{item_header}为空")
        grouped.setdefault(key, []).append((row_number, row))

    result: dict[tuple[str, str], CurrentItem] = {}
    for key, keyed_rows in grouped.items():
        latencies = [_decimal(row.get(latency_header, ""), latency_header) for _number, row in keyed_rows]
        if kind == "slow_service":
            count = sum(
                (_decimal(row.get("调用次数", ""), "调用次数") for _number, row in keyed_rows),
                Decimal(0),
            )
        else:
            count = Decimal(len(keyed_rows))
        fixed = FixedFields(
            owner=_latest_nonempty(keyed_rows, "责任人"),
            status=_latest_nonempty(keyed_rows, "处置状态"),
            solution=_latest_nonempty(keyed_rows, "处置方案"),
            remark=_latest_nonempty(keyed_rows, "备注（原因）"),
        )
        result[key] = CurrentItem(
            key=key,
            fixed=fixed,
            value=f"{_format_decimal(max(latencies))} / {_format_decimal(count)}",
        )
    return result


def plan_summary_update(
    kind: str,
    period: str,
    current_rows: list[dict[str, str]],
    existing_matrix: list[list[str]],
) -> SummaryUpdatePlan:
    if kind not in FIXED_HEADERS:
        raise ValueError(f"不支持的汇总类型: {kind}")
    if not existing_matrix:
        raise SummaryUpdateError("汇总页为空；历史初始化不属于 g3-inspection")

    header = [str(value or "").strip() for value in existing_matrix[0]]
    if header[:6] != FIXED_HEADERS[kind]:
        raise SummaryUpdateError(f"汇总页固定表头不匹配: {header[:6]}")
    period_headers = [value for value in header[6:] if value]
    duplicate_periods = sorted({value for value in period_headers if period_headers.count(value) > 1})
    if duplicate_periods:
        raise SummaryUpdateError(f"汇总页存在重复周期: {', '.join(duplicate_periods)}")

    if "" in header[7:]:
        raise SummaryUpdateError("汇总页历史周期表头中存在空列")

    if period in header[6:]:
        insert_period = False
        period_col = header.index(period, 6)
        target_width = len(header)
    elif len(header) > 6 and header[6] == "":
        # 恢复“已插入 G 列，但尚未写入周期表头”的中断状态。
        insert_period = False
        period_col = 6
        target_width = len(header)
    else:
        insert_period = True
        period_col = 6
        target_width = len(header) + 1

    existing_keys: dict[tuple[str, str], int] = {}
    for row_index, row in enumerate(existing_matrix[1:], start=1):
        padded = list(row) + [""] * max(0, 2 - len(row))
        key = (str(padded[0]).strip(), str(padded[1]).strip())
        if not any(key):
            continue
        if not all(key):
            raise SummaryUpdateError(f"汇总页第 {row_index + 1} 行唯一键不完整")
        if key in existing_keys:
            raise SummaryUpdateError(f"汇总页存在重复唯一键: {key[0]} + {key[1]}")
        existing_keys[key] = row_index

    current = aggregate_current_week(kind, current_rows)
    plan = SummaryUpdatePlan(kind, period, insert_period, period_col)
    next_row = len(existing_matrix)
    for key in sorted(current):
        item = current[key]
        if key in existing_keys:
            plan.value_updates[(existing_keys[key], period_col)] = item.value
            continue
        row = [""] * target_width
        row[:6] = [
            key[0],
            key[1],
            item.fixed.owner,
            item.fixed.status,
            item.fixed.solution,
            item.fixed.remark,
        ]
        row[period_col] = item.value
        plan.append_rows.append(row)
        plan.value_updates[(next_row, period_col)] = item.value
        next_row += 1
    return plan
