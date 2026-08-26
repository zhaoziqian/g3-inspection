#!/usr/bin/env python3
"""Refresh weekly slow-service and slow-SQL summary columns in Tencent Docs."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

try:
    from scripts.scan_summary import SummaryUpdateError, SummaryUpdatePlan, plan_summary_update
except ModuleNotFoundError:
    from scan_summary import SummaryUpdateError, SummaryUpdatePlan, plan_summary_update


TENCENT_FILE_ID = "DWHBzb1ZFZWhFREZa"
TARGETS = {
    "slow_service": {"source_prefix": "慢服务", "target_name": "慢服务汇总", "target_id": "z776s9"},
    "slow_sql": {"source_prefix": "慢SQL", "target_name": "慢SQL汇总", "target_id": "cczg56"},
}
REQUIRED_HEADERS = {
    "slow_service": ["时间窗口结束", "应用英文名", "服务名", "平均耗时(ms)", "调用次数", "责任人", "处置状态", "处置方案", "备注（原因）"],
    "slow_sql": ["时间窗口结束", "应用英文名", "SQL名", "耗时(ms)", "责任人", "处置状态", "处置方案", "备注（原因）"],
}


@dataclass(frozen=True)
class SheetMeta:
    sheet_id: str
    sheet_name: str
    row_count: int
    col_count: int


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def _contiguous_groups(columns: list[int]) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    for column in sorted(columns):
        if groups and column == groups[-1][1] + 1:
            groups[-1] = (groups[-1][0], column)
        else:
            groups.append((column, column))
    return groups


def read_required_rows(client, meta: SheetMeta, kind: str) -> list[dict[str, str]]:
    header_rows = _parse_csv(client.get_csv(meta.sheet_id, 0, 0, 0, meta.col_count - 1))
    header = header_rows[0] if header_rows else []
    columns = {str(value).strip(): index for index, value in enumerate(header) if str(value).strip()}
    required = REQUIRED_HEADERS[kind]
    missing = [name for name in required if name not in columns]
    if missing:
        raise SummaryUpdateError(f"{meta.sheet_name} 缺少必需表头: {', '.join(missing)}")

    selected = {name: columns[name] for name in required}
    values: dict[tuple[int, int], str] = {}
    for row_start in range(1, meta.row_count, 150):
        row_end = min(row_start + 149, meta.row_count - 1)
        for col_start, col_end in _contiguous_groups(list(selected.values())):
            rows = _parse_csv(client.get_csv(meta.sheet_id, row_start, row_end, col_start, col_end))
            for row_offset, row in enumerate(rows):
                for col_offset, value in enumerate(row):
                    values[(row_start + row_offset, col_start + col_offset)] = str(value).strip()

    result = []
    for row_index in range(1, meta.row_count):
        row = {name: values.get((row_index, column), "") for name, column in selected.items()}
        if any(row.values()):
            result.append(row)
    return result


def read_summary_matrix(client, meta: SheetMeta) -> list[list[str]]:
    matrix: list[list[str]] = []
    for row_start in range(0, meta.row_count, 150):
        row_end = min(row_start + 149, meta.row_count - 1)
        rows = _parse_csv(client.get_csv(meta.sheet_id, row_start, row_end, 0, meta.col_count - 1))
        matrix.extend(rows)
    while matrix and not any(str(value).strip() for value in matrix[-1]):
        matrix.pop()
    if not matrix:
        return []
    used_cols = max(
        (index + 1 for row in matrix for index, value in enumerate(row) if str(value).strip()),
        default=0,
    )
    return [
        [str(value).strip() for value in (row + [""] * used_cols)[:used_cols]]
        for row in matrix
    ]


def require_nonempty_source(rows: list[dict[str, str]], sheet_name: str, allow_empty: bool) -> None:
    if not rows and not allow_empty:
        raise SummaryUpdateError(
            f"{sheet_name} 没有数据行；如已确认本周确实为零记录，使用 --allow-empty"
        )


def _string_cell(row: int, col: int, value: str) -> dict[str, Any]:
    return {"row": row, "col": col, "value_type": "STRING", "string_value": value}


def apply_update_plan(
    client,
    meta: SheetMeta,
    existing_used_rows: int,
    plan: SummaryUpdatePlan,
) -> None:
    if plan.insert_period:
        client.insert_dimension(
            meta.sheet_id,
            dimension_type="col",
            index=plan.period_col,
            count=1,
            direction="before",
        )
    elif existing_used_rows > 1:
        client.clear_range(
            meta.sheet_id,
            start_row=1,
            end_row=existing_used_rows - 1,
            start_col=plan.period_col,
            end_col=plan.period_col,
        )

    final_rows = existing_used_rows + len(plan.append_rows)
    if final_rows > meta.row_count:
        client.insert_dimension(
            meta.sheet_id,
            dimension_type="row",
            index=max(meta.row_count - 1, 0),
            count=final_rows - meta.row_count,
            direction="after",
        )

    values = [_string_cell(0, plan.period_col, plan.period)]
    values.extend(
        _string_cell(row, col, value)
        for (row, col), value in sorted(plan.value_updates.items())
        if row < existing_used_rows
    )
    for offset, row_values in enumerate(plan.append_rows):
        row_index = existing_used_rows + offset
        values.extend(
            _string_cell(row_index, col_index, value)
            for col_index, value in enumerate(row_values)
            if value != ""
        )
    for start in range(0, len(values), 100):
        client.set_values(meta.sheet_id, values[start : start + 100])
    client.style_period_column(meta.sheet_id, plan.period_col, max(final_rows - 1, 0))


def _padded(row: list[str], width: int) -> list[str]:
    return [str(value).strip() for value in (list(row) + [""] * width)[:width]]


def verify_applied_update(
    before: list[list[str]],
    after: list[list[str]],
    plan: SummaryUpdatePlan,
) -> None:
    if not after or after[0].count(plan.period) != 1:
        raise SummaryUpdateError(f"写后校验失败：周期 {plan.period} 不唯一")
    if len(after[0]) <= plan.period_col or after[0][plan.period_col] != plan.period:
        raise SummaryUpdateError(f"写后校验失败：周期 {plan.period} 列位置不正确")

    before_width = len(before[0])
    after_width = len(after[0])
    for row_index in range(1, len(before)):
        old = _padded(before[row_index], before_width)
        new = _padded(after[row_index] if row_index < len(after) else [], after_width)
        if new[:6] != old[:6]:
            raise SummaryUpdateError(f"写后校验失败：第 {row_index + 1} 行固定字段发生变化")
        if plan.insert_period:
            preserved = new[: plan.period_col] + new[plan.period_col + 1 :]
        else:
            preserved = list(new)
            preserved[plan.period_col] = old[plan.period_col]
        if _padded(preserved, before_width) != old:
            raise SummaryUpdateError(f"写后校验失败：第 {row_index + 1} 行历史数据发生变化")

    for (row_index, col_index), expected in plan.value_updates.items():
        actual_row = after[row_index] if row_index < len(after) else []
        actual = actual_row[col_index].strip() if col_index < len(actual_row) else ""
        if actual != expected:
            raise SummaryUpdateError(
                f"写后校验失败：R{row_index + 1}C{col_index + 1} 期望 {expected!r}，实际 {actual!r}"
            )


class TencentSummaryClient:
    def __init__(self, file_id: str = TENCENT_FILE_ID):
        self.file_id = file_id

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = dict(arguments)
        args["file_id"] = self.file_id
        result = subprocess.run(
            ["mcporter", "call", "sheet-mcp", tool, "--args", json.dumps(args, ensure_ascii=False)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SummaryUpdateError(result.stderr.strip() or result.stdout.strip())
        output = result.stdout.strip()
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            start, end = output.find("{"), output.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(output[start : end + 1])
                except json.JSONDecodeError:
                    pass
                else:
                    if data.get("error"):
                        raise SummaryUpdateError(str(data["error"]))
                    return data
            raise SummaryUpdateError(f"sheet-mcp {tool} 返回无效 JSON: {output[:300]}") from None
        if data.get("error"):
            raise SummaryUpdateError(str(data["error"]))
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

    def clear_range(self, sheet_id: str, **arguments) -> None:
        self.call("clear_range_cells", {"sheet_id": sheet_id, **arguments})

    def set_values(self, sheet_id: str, values: list[dict[str, Any]]) -> None:
        self.call("set_range_value", {"sheet_id": sheet_id, "values": values})

    def style_period_column(self, sheet_id: str, column: int, end_row: int) -> None:
        self.call(
            "set_cell_style",
            {
                "sheet_id": sheet_id,
                "start_row": 0,
                "end_row": 0,
                "start_col": column,
                "end_col": column,
                "bold": True,
                "bg_color": "FF4472C4",
                "font_color": "FFFFFFFF",
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
                    "start_col": column,
                    "end_col": column,
                    "horizontal_align": "center",
                    "vertical_align": "top",
                    "wrap_text": True,
                },
            )
        self.call(
            "set_dimension_size",
            {"sheet_id": sheet_id, "dimensions": [{"dimension_type": "col", "index": column, "size": 110}]},
        )


def _meta(raw: dict[str, Any]) -> SheetMeta:
    return SheetMeta(str(raw["sheet_id"]), str(raw["sheet_name"]), int(raw["row_count"]), int(raw["col_count"]))


def run_refresh(
    client,
    period: str,
    apply: bool = False,
    allow_empty: bool = False,
) -> dict[str, dict[str, Any]]:
    if not re.fullmatch(r"\d{4}-\d{4}", period):
        raise SummaryUpdateError("周期格式必须为 MMDD-MMDD")
    by_name = {sheet["sheet_name"]: _meta(sheet) for sheet in client.sheet_info()}
    plans: dict[str, tuple[SheetMeta, list[list[str]], SummaryUpdatePlan]] = {}
    report: dict[str, dict[str, Any]] = {}

    for kind, config in TARGETS.items():
        source_name = f"{config['source_prefix']}{period}"
        source = by_name.get(source_name)
        target = by_name.get(config["target_name"])
        if not source:
            raise SummaryUpdateError(f"找不到当前周 sheet: {source_name}")
        if not target or target.sheet_id != config["target_id"]:
            raise SummaryUpdateError(f"找不到目标汇总页或 sheet ID 不匹配: {config['target_name']}")
        current_rows = read_required_rows(client, source, kind)
        require_nonempty_source(current_rows, source_name, allow_empty)
        existing = read_summary_matrix(client, target)
        plan = plan_summary_update(kind, period, current_rows, existing)
        plans[kind] = (target, existing, plan)
        report[kind] = {
            "source_rows": len(current_rows),
            "aggregated_items": len(plan.value_updates),
            "new_items": len(plan.append_rows),
            "insert_period": plan.insert_period,
            "period_col": plan.period_col,
        }

    if apply:
        for kind in ("slow_service", "slow_sql"):
            target, existing, plan = plans[kind]
            apply_update_plan(client, target, len(existing), plan)
        refreshed = {sheet["sheet_name"]: _meta(sheet) for sheet in client.sheet_info()}
        for kind in ("slow_service", "slow_sql"):
            _target, existing, plan = plans[kind]
            fresh_target = refreshed[TARGETS[kind]["target_name"]]
            after = read_summary_matrix(client, fresh_target)
            verify_applied_update(existing, after, plan)
            report[kind]["verified"] = True
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="持续更新腾讯文档慢服务/慢SQL汇总页。默认 dry-run。")
    parser.add_argument("--period", required=True, help="周期 MMDD-MMDD，例如 0824-0830")
    parser.add_argument("--apply", action="store_true", help="实际写入腾讯文档")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="明确允许当周明细为零行（默认拒绝，防止慢SQL忘记粘贴）",
    )
    parser.add_argument("--file-id", default=TENCENT_FILE_ID)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        report = run_refresh(
            TencentSummaryClient(args.file_id),
            args.period,
            args.apply,
            args.allow_empty,
        )
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
