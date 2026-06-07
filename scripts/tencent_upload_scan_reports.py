#!/usr/bin/env python3
"""
week-tencent-upload: 将周巡检慢服务/慢SQL扫描报告上传到腾讯文档。

工作流：
  1. 在目录 sheet 写入本周条目（含 set_link 跳转）
  2. 在文档最前面创建 慢服务MMDD-MMDD 和 慢SQLMMDD-MMDD 两个新 sheet
  3. 写入慢服务数据（全量，含样式表头）
  4. 只写慢SQL表头（含样式），数据由用户手动粘贴
  5. 打印用户操作说明

用法：
  python3 tencent_upload_scan_reports.py <period_dir>
  # period_dir 示例：/path/to/20260601-20260607
"""
import csv
import json
import os
import re
import subprocess
import sys
import glob

# ── 腾讯文档配置 ──────────────────────────────────────────────────────────────
TENCENT_FILE_ID = "DWHBzb1ZFZWhFREZa"
TENCENT_FILE_URL = "https://docs.qq.com/sheet/DWHBzb1ZFZWhFREZa"
DIRECTORY_SHEET_ID = "BB08J2"

# 目录 sheet 中周期/报告列的位置（0-based）
DIR_COL_PERIOD = 12
DIR_COL_SLOW_SVC = 13
DIR_COL_SLOW_SQL = 14

# 表头样式
HEADER_BG_COLOR = "FF8CDDFA"

# 列宽（像素）：11列，顺序与各 sheet 表头一致
SVC_COL_WIDTHS = [140, 140, 140, 340, 100,  90, 100, 90, 90, 350, 110]
SQL_COL_WIDTHS = [140, 140, 140, 400, 110, 100, 100, 90, 90, 350, 110]
ROW_HEIGHT = 24  # 行高

# 慢服务列定义（含新增"链路详情"列）
SLOW_SVC_HEADERS = [
    "时间窗口开始", "时间窗口结束", "应用英文名", "服务名",
    "平均耗时(ms)", "调用次数", "链路详情",
    "责任人", "处置状态", "处置方案", "备注（原因）"
]

# 慢SQL列定义（含新增"链路详情"列）
SLOW_SQL_HEADERS = [
    "时间窗口开始", "时间窗口结束", "应用英文名", "SQL名",
    "SQL语句", "耗时(ms)", "链路详情",
    "责任人", "处置状态", "处置方案", "备注（原因）"
]


# ── mcporter 调用 ─────────────────────────────────────────────────────────────

def mcporter(service, tool, args_dict):
    payload = json.dumps(args_dict, ensure_ascii=False)
    result = subprocess.run(
        ["mcporter", "call", service, tool, "--args", payload],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  ✗ {service}.{tool} 失败: {result.stderr.strip()[:300]}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def sheetengine(tool, args_dict):
    return mcporter("tencent-sheetengine", tool, args_dict)


# ── 辅助函数 ─────────────────────────────────────────────────────────────────

def make_cell(row, col, value):
    try:
        num = float(value)
        return {"row": row, "col": col, "value_type": "NUMBER", "number_value": num}
    except (ValueError, TypeError):
        return {"row": row, "col": col, "value_type": "STRING",
                "string_value": str(value) if value else ""}


def write_values(sheet_id, values):
    sheetengine("set_range_value", {
        "file_id": TENCENT_FILE_ID,
        "sheet_id": sheet_id,
        "values": values
    })


def write_header_with_style(sheet_id, headers):
    """写标题行数据 + 蓝色加粗样式。"""
    values = [make_cell(0, c, h) for c, h in enumerate(headers)]
    write_values(sheet_id, values)
    sheetengine("set_cell_style", {
        "file_id": TENCENT_FILE_ID,
        "sheet_id": sheet_id,
        "start_row": 0, "start_col": 0,
        "end_row": 0, "end_col": len(headers) - 1,
        "bg_color": HEADER_BG_COLOR,
        "bold": True
    })


def set_sheet_dimensions(sheet_id, col_widths, wrap_cols=None):
    """设置列宽和行高；wrap_cols 为需整列开启行内换行的列索引列表。"""
    info = sheetengine("get_sheet_info", {"file_id": TENCENT_FILE_ID})
    row_count = next((s["row_count"] for s in info["sheets"] if s["sheet_id"] == sheet_id), 100)
    dimensions = [{"dimension_type": "col", "index": i, "size": w} for i, w in enumerate(col_widths)]
    dimensions += [{"dimension_type": "row", "index": r, "size": ROW_HEIGHT} for r in range(row_count)]
    sheetengine("set_dimension_size", {
        "file_id": TENCENT_FILE_ID,
        "sheet_id": sheet_id,
        "dimensions": dimensions
    })
    for col in (wrap_cols or []):
        sheetengine("set_cell_style", {
            "file_id": TENCENT_FILE_ID,
            "sheet_id": sheet_id,
            "start_row": 0, "start_col": col,
            "end_row": row_count - 1, "end_col": col,
            "wrap_text": True
        })


def get_existing_sheet_names():
    info = sheetengine("get_sheet_info", {"file_id": TENCENT_FILE_ID})
    return {s["sheet_name"]: s["sheet_id"] for s in info["sheets"]}


def sheet_has_data(sheet_id):
    """检查 sheet 是否已有数据（不含空表头行）。返回 (has_data, row_count)。"""
    data = sheetengine("get_cell_data", {
        "file_id": TENCENT_FILE_ID,
        "sheet_id": sheet_id,
        "start_row": 1, "start_col": 0,
        "end_row": 3, "end_col": 0,   # 只查前几行，够判断即可
        "return_csv": True
    })
    lines = [l for l in data["csv_data"].strip().split("\n") if l.strip().strip(",")]
    return len(lines) > 0, len(lines)


def find_dir_row(period_short):
    """在目录 sheet 中查找周期行；已存在返回行号，否则返回下一个空行号。"""
    data = sheetengine("get_cell_data", {
        "file_id": TENCENT_FILE_ID,
        "sheet_id": DIRECTORY_SHEET_ID,
        "start_row": 1, "start_col": DIR_COL_PERIOD,
        "end_row": 20, "end_col": DIR_COL_PERIOD,
        "return_csv": True
    })
    lines = data["csv_data"].strip().split("\n")
    for i, line in enumerate(lines):
        val = line.strip().strip(",")
        if val == period_short:
            return 1 + i, True   # (row, already_exists)
        if not val:
            return 1 + i, False  # 第一个空行
    return 1 + len(lines), False


def confirm_overwrite(label):
    """向用户确认是否覆盖已有内容，返回 True 表示继续。"""
    sys.stdout.flush()
    try:
        ans = input(f"\n  ⚠️  {label} 已有数据，是否覆盖？[y/N] ").strip().lower()
    except EOFError:
        ans = "n"
    return ans in ("y", "yes")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("用法: python3 tencent_upload_scan_reports.py <period_dir>")
        print("示例: python3 tencent_upload_scan_reports.py /path/to/20260601-20260607")
        sys.exit(1)

    period_dir = sys.argv[1].rstrip("/")
    if not os.path.isdir(period_dir):
        print(f"错误：目录不存在: {period_dir}", file=sys.stderr)
        sys.exit(1)

    # 从目录名解析周期（YYYYMMDD-YYYYMMDD → MMDD-MMDD）
    dir_name = os.path.basename(period_dir)
    m = re.match(r"(\d{4})(\d{2})(\d{2})-\d{4}(\d{2})(\d{2})", dir_name)
    if not m:
        print(f"错误：目录名格式不对，期望 YYYYMMDD-YYYYMMDD，实际: {dir_name}", file=sys.stderr)
        sys.exit(1)
    period_short = f"{m.group(2)}{m.group(3)}-{m.group(4)}{m.group(5)}"  # e.g. 0601-0607

    svc_sheet_name = f"慢服务{period_short}"
    sql_sheet_name = f"慢SQL{period_short}"

    # 查找 CSV 源文件
    svc_csv_pattern = os.path.join(period_dir, "慢服务报告", "slow_analysis_*.csv")
    sql_csv_pattern = os.path.join(period_dir, "数据库状态检查", "sql_analysis_*.csv")
    svc_csvs = glob.glob(svc_csv_pattern)
    sql_csvs = glob.glob(sql_csv_pattern)

    if not svc_csvs:
        print(f"错误：找不到慢服务 CSV: {svc_csv_pattern}", file=sys.stderr)
        sys.exit(1)
    if not sql_csvs:
        print(f"错误：找不到慢SQL CSV: {sql_csv_pattern}", file=sys.stderr)
        sys.exit(1)

    svc_csv = svc_csvs[0]
    sql_csv = sql_csvs[0]

    print(f"周期: {period_short}  ({dir_name})")
    print(f"慢服务 CSV: {os.path.basename(svc_csv)}")
    print(f"慢SQL  CSV: {os.path.basename(sql_csv)}")
    print()

    # ── Step 1: 检查 sheet 是否已存在，有数据则询问是否覆盖 ───────────────────
    print("Step 1: 检查已有 sheet...")
    existing = get_existing_sheet_names()

    svc_sheet_id = existing.get(svc_sheet_name)
    sql_sheet_id = existing.get(sql_sheet_name)

    # 覆盖标记：默认写入，已有数据时由用户决定
    write_svc = True
    write_sql_header = True

    if svc_sheet_id:
        has_data, row_count = sheet_has_data(svc_sheet_id)
        if has_data:
            print(f"  ! {svc_sheet_name} 已存在且有数据（约 {row_count}+ 行）")
            write_svc = confirm_overwrite(svc_sheet_name)
            if not write_svc:
                print(f"  → 跳过 {svc_sheet_name} 写入")
        else:
            print(f"  ✓ {svc_sheet_name} 已存在（空表），将写入数据")
    else:
        print(f"  {svc_sheet_name} 不存在，将新建")

    if sql_sheet_id:
        has_data, row_count = sheet_has_data(sql_sheet_id)
        if has_data:
            print(f"  ! {sql_sheet_name} 已存在且有数据（约 {row_count}+ 行）")
            write_sql_header = confirm_overwrite(f"{sql_sheet_name} 表头")
            if not write_sql_header:
                print(f"  → 跳过 {sql_sheet_name} 表头重写")
        else:
            print(f"  ✓ {sql_sheet_name} 已存在（空表），将写入表头")
    else:
        print(f"  {sql_sheet_name} 不存在，将新建")

    # ── Step 2: 创建两个新 sheet（排在最前，index=1）──────────────────────────
    if not svc_sheet_id:
        print(f"\nStep 2a: 创建 {svc_sheet_name}...")
        res = sheetengine("add_sheet", {
            "file_id": TENCENT_FILE_ID,
            "name": svc_sheet_name,
            "index": 1          # 排在 目录(0) 之后，现有 sheet 之前
        })
        svc_sheet_id = res["sheet_id"]
        print(f"  ✓ 已创建  sheet_id={svc_sheet_id}")

    if not sql_sheet_id:
        print(f"Step 2b: 创建 {sql_sheet_name}...")
        res = sheetengine("add_sheet", {
            "file_id": TENCENT_FILE_ID,
            "name": sql_sheet_name,
            "index": 2          # 排在 慢服务 之后
        })
        sql_sheet_id = res["sheet_id"]
        print(f"  ✓ 已创建  sheet_id={sql_sheet_id}")

    # ── Step 3: 目录 sheet 写入本周条目 + 跳转链接 ────────────────────────────
    print("\nStep 3: 更新目录 sheet...")
    target_row, dir_exists = find_dir_row(period_short)

    if dir_exists:
        print(f"  ! 目录第 {target_row+1} 行已有 {period_short} 条目")
        if not confirm_overwrite(f"目录 {period_short} 条目"):
            print("  → 跳过目录更新")
            dir_exists = None   # 用 None 标记"已跳过"

    if dir_exists is not None:  # None=跳过，False/True=需要写入
        svc_url = f"{TENCENT_FILE_URL}?tab={svc_sheet_id}"
        sql_url = f"{TENCENT_FILE_URL}?tab={sql_sheet_id}"

        write_values(DIRECTORY_SHEET_ID, [
            make_cell(target_row, DIR_COL_PERIOD, period_short)
        ])
        sheetengine("set_link", {
            "file_id": TENCENT_FILE_ID,
            "sheet_id": DIRECTORY_SHEET_ID,
            "row": target_row, "col": DIR_COL_SLOW_SVC,
            "url": svc_url,
            "display_text": svc_sheet_name
        })
        sheetengine("set_link", {
            "file_id": TENCENT_FILE_ID,
            "sheet_id": DIRECTORY_SHEET_ID,
            "row": target_row, "col": DIR_COL_SLOW_SQL,
            "url": sql_url,
            "display_text": sql_sheet_name
        })
        print(f"  ✓ 目录第 {target_row+1} 行写入完成（含跳转链接）")

    # ── Step 4: 慢服务 sheet：表头 + 全量数据 ────────────────────────────────
    print(f"\nStep 4: 写入 {svc_sheet_name}...")
    if write_svc:
        write_header_with_style(svc_sheet_id, SLOW_SVC_HEADERS)
        print("  ✓ 表头写入完成")

        with open(svc_csv, encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader)  # 跳过原始标题
            rows = list(reader)

        BATCH = 30
        total = len(rows)
        for start in range(0, total, BATCH):
            batch = rows[start:start + BATCH]
            values = []
            for r_off, row in enumerate(batch):
                sheet_row = 1 + start + r_off
                padded = (row[:len(SLOW_SVC_HEADERS)] + [""] * len(SLOW_SVC_HEADERS))[:len(SLOW_SVC_HEADERS)]
                for c, val in enumerate(padded):
                    values.append(make_cell(sheet_row, c, val))
            write_values(svc_sheet_id, values)
            end = min(start + BATCH, total)
            print(f"  ✓ 第 {start+1}-{end} 行写入完成")

        print(f"  共写入 {total} 条慢服务数据")

        # 批量填充"处置状态"列为"待处理"
        status_col = SLOW_SVC_HEADERS.index("处置状态")
        status_values = [
            {"row": r, "col": status_col, "value_type": "STRING", "string_value": "待处理"}
            for r in range(1, total + 1)
        ]
        write_values(svc_sheet_id, status_values)
        print(f"  ✓ 处置状态列已填充「待处理」（{total} 行）")

        set_sheet_dimensions(svc_sheet_id, SVC_COL_WIDTHS, wrap_cols=[6])  # 链路详情列行内换行
        print("  ✓ 列宽/行高/换行设置完成")
    else:
        print(f"  → 已跳过（用户选择不覆盖）")

    # ── Step 5: 慢SQL sheet：仅写表头 ────────────────────────────────────────
    print(f"\nStep 5: 写入 {sql_sheet_name} 表头...")
    if write_sql_header:
        write_header_with_style(sql_sheet_id, SLOW_SQL_HEADERS)
        set_sheet_dimensions(sql_sheet_id, SQL_COL_WIDTHS, wrap_cols=[4, 6])  # SQL语句/链路详情列行内换行
        print("  ✓ 表头写入完成（含蓝色加粗样式、列宽/行高/换行）")
    else:
        print(f"  → 已跳过（用户选择不覆盖）")

    # 统计 SQL CSV 行数供用户参考（用 csv.reader 正确处理字段内换行）
    with open(sql_csv, encoding="utf-8-sig") as f:
        sql_row_count = sum(1 for _ in csv.reader(f)) - 1  # 减去标题行

    # ── 完成，打印用户操作说明 ────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("✅ 自动化步骤完成！")
    print()
    print("📋 接下来需要手动完成慢SQL数据粘贴：")
    print()
    print(f"  1. 打开腾讯文档：{TENCENT_FILE_URL}?tab={sql_sheet_id}")
    print(f"  2. 切换到 [{sql_sheet_name}] 标签页")
    print(f"  3. 点击第 2 行第 1 列（A2）单元格")
    print(f"  4. 打开本地 CSV 文件：")
    print(f"     {sql_csv}")
    print(f"  5. 复制除第 1 行（标题行）以外的所有内容（共 {sql_row_count} 行）")
    print(f"  6. 粘贴到腾讯文档 A2 单元格")
    print()
    print("  ⚠️  注意：列顺序需与表头一致：")
    print(f"     {' | '.join(SLOW_SQL_HEADERS)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
