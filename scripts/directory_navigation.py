"""Fixed rolling navigation for the Tencent inspection workbook directory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol
from urllib.parse import parse_qs, urlparse

try:
    from scripts.scan_archive import ArchiveError, select_archive_periods
except ModuleNotFoundError:
    from scan_archive import ArchiveError, select_archive_periods


TENCENT_FILE_URL = "https://docs.qq.com/sheet/DWHBzb1ZFZWhFREZa"
DIRECTORY_SHEET_ID = "BB08J2"
FIXED_SHEETS = {
    "目录": DIRECTORY_SHEET_ID,
    "慢服务汇总": "z776s9",
    "慢SQL汇总": "cczg56",
    "慢服务归档": "7i67j3",
    "慢SQL归档": "8i29ez",
}


class NavigationError(RuntimeError):
    """Raised when fixed directory navigation cannot be safely rebuilt."""


class NavigationClient(Protocol):
    def set_values(self, sheet_id: str, values: list[dict[str, Any]]) -> None: ...

    def set_link(
        self,
        sheet_id: str,
        row: int,
        col: int,
        url: str,
        display_text: str,
    ) -> None: ...

    def get_cells(
        self,
        sheet_id: str,
        start_row: int,
        end_row: int,
        start_col: int,
        end_col: int,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class NavigationLink:
    row: int
    col: int
    display_text: str
    sheet_id: str


@dataclass(frozen=True)
class NavigationPlan:
    rows: tuple[tuple[str, str, str], ...]
    links: tuple[NavigationLink, ...]


def _sheet_map(sheets: Iterable[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for sheet in sheets:
        name = str(sheet.get("sheet_name", ""))
        sheet_id = str(sheet.get("sheet_id", ""))
        if name in result:
            raise NavigationError(f"Sheet 名称重复: {name}")
        result[name] = sheet_id
    return result


def build_navigation_plan(
    sheets: Iterable[dict[str, Any]],
    *,
    keep_weeks: int = 5,
) -> NavigationPlan:
    materialized = list(sheets)
    by_name = _sheet_map(materialized)
    for name, expected_id in FIXED_SHEETS.items():
        actual_id = by_name.get(name)
        if actual_id is None:
            raise NavigationError(f"缺少固定 Sheet: {name}")
        if actual_id != expected_id:
            raise NavigationError(f"{name} 的 Sheet ID 不匹配: {actual_id}")

    try:
        selection = select_archive_periods(by_name, keep_weeks=keep_weeks)
    except ArchiveError as exc:
        raise NavigationError(str(exc)) from exc
    if selection.unpaired:
        raise NavigationError(f"存在非配对周期: {', '.join(selection.unpaired)}")
    if len(selection.keep) != keep_weeks:
        raise NavigationError(
            f"目录需要 {keep_weeks} 个完整周期，实际 {len(selection.keep)} 个"
        )

    rows: list[tuple[str, str, str]] = [
        ("周期", "慢服务扫描报告", "慢SQL扫描报告"),
        ("", "慢服务汇总", "慢SQL汇总"),
    ]
    rows.extend(
        (period, f"慢服务{period}", f"慢SQL{period}")
        for period in selection.keep
    )
    rows.append(("", "慢服务归档", "慢SQL归档"))

    links: list[NavigationLink] = []
    for row, (_period, service_name, sql_name) in enumerate(rows[1:], start=1):
        for col, name in ((13, service_name), (14, sql_name)):
            sheet_id = by_name.get(name)
            if not sheet_id:
                raise NavigationError(f"目录目标 Sheet 不存在: {name}")
            links.append(NavigationLink(row, col, name, sheet_id))
    return NavigationPlan(tuple(rows), tuple(links))


def _target_sheet_id(url: str) -> str:
    value = str(url)
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or parsed.query:
        tabs = parse_qs(parsed.query).get("tab", [])
        return tabs[0] if tabs else value
    return value


def _verify_navigation(cells: Iterable[dict[str, Any]], plan: NavigationPlan) -> None:
    by_position = {
        (int(cell["row"]), int(cell["col"])): cell
        for cell in cells
    }
    for row, values in enumerate(plan.rows):
        for offset, expected in enumerate(values):
            col = 12 + offset
            actual = str(by_position.get((row, col), {}).get("string_value", ""))
            if actual != expected:
                raise NavigationError(
                    f"目录文本校验失败: ({row}, {col}) 期望 {expected!r}，实际 {actual!r}"
                )
    for expected in plan.links:
        cell = by_position.get((expected.row, expected.col), {})
        hyperlinks = cell.get("hyperlinks") or []
        if len(hyperlinks) != 1:
            raise NavigationError(
                f"目录链接校验失败: ({expected.row}, {expected.col}) 链接数量不是 1"
            )
        hyperlink = hyperlinks[0]
        if (
            str(hyperlink.get("text", "")) != expected.display_text
            or _target_sheet_id(str(hyperlink.get("url", ""))) != expected.sheet_id
        ):
            raise NavigationError(
                f"目录链接校验失败: ({expected.row}, {expected.col}) 目标不匹配"
            )


def rebuild_directory_navigation(
    client: NavigationClient,
    sheets: Iterable[dict[str, Any]],
    *,
    directory_sheet_id: str = DIRECTORY_SHEET_ID,
) -> NavigationPlan:
    if directory_sheet_id != DIRECTORY_SHEET_ID:
        raise NavigationError(f"目录 Sheet ID 不匹配: {directory_sheet_id}")
    plan = build_navigation_plan(sheets)
    cells = [
        {
            "row": row,
            "col": 12 + offset,
            "value_type": "STRING",
            "string_value": value,
        }
        for row, values in enumerate(plan.rows)
        for offset, value in enumerate(values)
    ]
    client.set_values(directory_sheet_id, cells)
    for link in plan.links:
        client.set_link(
            directory_sheet_id,
            link.row,
            link.col,
            f"{TENCENT_FILE_URL}?tab={link.sheet_id}",
            link.display_text,
        )
    actual = client.get_cells(directory_sheet_id, 0, 7, 12, 14)
    _verify_navigation(actual, plan)
    return plan
