#!/usr/bin/env python3
import argparse
import subprocess
from pathlib import Path


DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def find_one_html(sql_dir):
    files = sorted(sql_dir.glob("*.html"))
    if len(files) != 1:
        raise SystemExit(f"数据库状态检查目录必须有且只有 1 个 HTML 文件，当前 {len(files)} 个: {sql_dir}")
    return files[0]


def period_from_dir(period_dir):
    period = period_dir.name
    if len(period) == 17 and period[8] == "-":
        return period
    raise SystemExit(f"周期目录名不符合 YYYYMMDD-YYYYMMDD: {period}")


def run(cmd):
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(description="从慢 SQL HTML 报告生成邮件用图表 PNG")
    parser.add_argument("period_dir", help="周巡检周期目录，例如 20260511-20260517")
    parser.add_argument("--chrome", default=DEFAULT_CHROME, help="Chrome 可执行文件路径")
    parser.add_argument("--width", type=int, default=1400, help="截图窗口宽度")
    parser.add_argument("--height", type=int, default=520, help="截图窗口高度")
    parser.add_argument("--crop-width", type=int, default=1360, help="裁剪宽度")
    parser.add_argument("--crop-height", type=int, default=282, help="裁剪高度")
    parser.add_argument("--crop-x", type=int, default=20, help="裁剪 X 偏移")
    parser.add_argument("--crop-y", type=int, default=117, help="裁剪 Y 偏移")
    args = parser.parse_args()

    period_dir = Path(args.period_dir).resolve()
    period = period_from_dir(period_dir)
    sql_dir = period_dir / "数据库状态检查"
    html_path = find_one_html(sql_dir)

    full_png = sql_dir / f"慢SQL分析图表_{period}_full.png"
    chart_png = sql_dir / f"慢SQL分析图表_{period}.png"

    run([
        args.chrome,
        "--headless",
        "--disable-gpu",
        "--allow-file-access-from-files",
        f"--window-size={args.width},{args.height}",
        "--virtual-time-budget=5000",
        f"--screenshot={full_png}",
        html_path.resolve().as_uri(),
    ])

    run([
        "sips",
        str(full_png),
        "--cropToHeightWidth",
        str(args.crop_height),
        str(args.crop_width),
        "--cropOffset",
        str(args.crop_y),
        str(args.crop_x),
        "--out",
        str(chart_png),
    ])

    if not chart_png.is_file() or chart_png.stat().st_size == 0:
        raise SystemExit(f"慢 SQL 图表 PNG 生成失败: {chart_png}")

    print(f"已生成慢 SQL 图表: {chart_png}")


if __name__ == "__main__":
    main()
