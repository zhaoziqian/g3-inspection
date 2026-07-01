#!/usr/bin/env python3
import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


DEFAULT_ITEMS = [
    ("程序健壮性", 30),
    ("程序设计漏洞", 18),
    ("设计交互优化", 66),
    ("需求更改", 7),
    ("业务逻辑问题", 23),
]

DEFAULT_COLORS = [
    "#25A47C",
    "#F5A623",
    "#3D8EDB",
    "#7C89E8",
    "#E94B4B",
    "#8B5CF6",
    "#14B8A6",
    "#F97316",
]


def parse_period_name(output_dir):
    period = output_dir.name
    if len(period) == 17 and period[8] == "-":
        start, end = period.split("-", 1)
        if start.isdigit() and end.isdigit() and len(start) == 8 and len(end) == 8:
            return period
    if len(period) == 9 and period[4] == "-":
        start, end = period.split("-", 1)
        if start.isdigit() and end.isdigit() and len(start) == 4 and len(end) == 4:
            return period
    raise SystemExit(f"目录名不符合 YYYYMMDD-YYYYMMDD 或 MMDD-MMDD: {period}")


def parse_item(raw):
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"数据项格式应为 名称=数量: {raw}")
    name, value = raw.split("=", 1)
    name = name.strip()
    value = value.strip()
    if not name:
        raise argparse.ArgumentTypeError(f"数据项名称不能为空: {raw}")
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"数据项数量必须为整数: {raw}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError(f"数据项数量必须大于 0: {raw}")
    return name, number


def resolve_output(args):
    if args.output:
        return Path(args.output).expanduser().resolve()

    if args.output_dir:
        output_dir = Path(args.output_dir).expanduser().resolve()
        period = parse_period_name(output_dir)
        return output_dir / f"问题类型分布饼图_{period}.png"

    return Path.cwd() / "问题类型分布饼图.png"


def configure_fonts():
    plt.rcParams["font.sans-serif"] = [
        "Arial Unicode MS",
        "PingFang SC",
        "Heiti TC",
        "Songti SC",
        "SimHei",
        "Microsoft YaHei",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False


def render_chart(items, output_path, title):
    labels = [name for name, _ in items]
    values = [value for _, value in items]
    total = sum(values)
    colors = [DEFAULT_COLORS[i % len(DEFAULT_COLORS)] for i in range(len(items))]

    configure_fonts()

    fig, ax = plt.subplots(figsize=(9.6, 6.4), dpi=200, facecolor="none")
    fig.patch.set_alpha(0)
    ax.set_facecolor("none")
    ax.set_aspect("equal")

    wedges, _ = ax.pie(
        values,
        startangle=90,
        counterclock=False,
        colors=colors,
        wedgeprops=dict(width=0.48, edgecolor="white", linewidth=2.2),
    )

    ax.add_artist(plt.Circle((0, 0), 0.34, fc=(1, 1, 1, 0), ec=(1, 1, 1, 0)))

    for wedge, label, value, color in zip(wedges, labels, values, colors):
        angle = (wedge.theta2 + wedge.theta1) / 2
        x = np.cos(np.deg2rad(angle))
        y = np.sin(np.deg2rad(angle))

        start_x, start_y = 0.98 * x, 0.98 * y
        mid_x, mid_y = 1.17 * x, 1.17 * y
        horizontal = 0.34 if x >= 0 else -0.34
        end_x, end_y = mid_x + horizontal, mid_y

        ax.plot(
            [start_x, mid_x, end_x],
            [start_y, mid_y, end_y],
            color=color,
            lw=1.8,
            solid_capstyle="round",
        )

        align = "left" if x >= 0 else "right"
        text_x = end_x + (0.035 if x >= 0 else -0.035)
        percent = value / total * 100
        ax.text(
            text_x,
            end_y + 0.035,
            label,
            ha=align,
            va="bottom",
            fontsize=12.5,
            color="#2B2B2B",
            weight="bold",
        )
        ax.text(
            text_x,
            end_y - 0.035,
            f"{value}项 - {percent:.1f}%",
            ha=align,
            va="top",
            fontsize=11,
            color="#555555",
        )

    ax.text(0, 0.035, f"{total}", ha="center", va="center", fontsize=22, weight="bold", color="#222222")
    ax.text(0, -0.11, "总计", ha="center", va="center", fontsize=10.5, color="#666666")

    ax.set_title(title, fontsize=18, weight="bold", color="#222222", pad=18)
    ax.set_xlim(-1.75, 1.75)
    ax.set_ylim(-1.35, 1.35)
    ax.axis("off")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.25, transparent=True)
    plt.close(fig)

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise SystemExit(f"问题类型分布饼图生成失败: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="生成问题类型分布环形饼图 PNG")
    parser.add_argument(
        "output_dir",
        nargs="?",
        help="输出目录。周巡检目录用 YYYYMMDD-YYYYMMDD；慢服务/慢SQL横向对比目录用 MMDD-MMDD。",
    )
    parser.add_argument(
        "--item",
        action="append",
        type=parse_item,
        help="问题类型数据项，格式为 名称=数量。可重复传入；不传则使用默认 5 项。",
    )
    parser.add_argument("--output", help="输出 PNG 路径；不传则输出到周期目录或当前目录")
    parser.add_argument("--title", default="问题类型分布", help="图表标题")
    args = parser.parse_args()

    items = args.item or DEFAULT_ITEMS
    output_path = resolve_output(args)
    render_chart(items, output_path, args.title)
    print(f"已生成问题类型分布饼图: {output_path}")


if __name__ == "__main__":
    main()
