#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "references" / "weekly_inspection.json"


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_period(period_dir):
    period = period_dir.name
    if len(period) != 17 or period[8] != "-":
        raise SystemExit(f"周期目录名不符合 YYYYMMDD-YYYYMMDD: {period}")
    start, end = period.split("-", 1)
    if not (start.isdigit() and end.isdigit() and len(start) == 8 and len(end) == 8):
        raise SystemExit(f"周期目录名不符合 YYYYMMDD-YYYYMMDD: {period}")
    return period


def require_file(path):
    if not path.is_file():
        raise SystemExit(f"缺少邮件素材文件: {path}")


def build_html(project_name, period, ledger_url, images):
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{project_name}{period}周巡检报告</title>
  <style>
    body {{
      margin: 0;
      padding: 0;
      color: #1f2933;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      font-size: 14px;
      line-height: 1.7;
      background: #ffffff;
    }}
    .mail-body {{
      max-width: 960px;
      padding: 16px 0;
    }}
    h2 {{
      margin: 18px 0 8px;
      font-size: 16px;
      color: #111827;
    }}
    h3 {{
      margin: 14px 0 8px;
      font-size: 14px;
      color: #374151;
    }}
    p {{
      margin: 0 0 10px;
    }}
    ul {{
      margin: 0 0 12px 20px;
      padding: 0;
    }}
    .chart {{
      margin: 8px 0 18px;
    }}
    .chart img {{
      max-width: 100%;
      height: auto;
      border: 1px solid #e5e7eb;
      display: block;
    }}
    .note {{
      margin-top: 16px;
      color: #4b5563;
    }}
    .link {{
      color: #2563eb;
    }}
  </style>
</head>
<body>
  <div class="mail-body">
    <p>各位领导、同事好：</p>
    <p>以下是{project_name}{period}周巡检报告。</p>

    <h2>内容梗概</h2>

    <h3>生产数据变更</h3>
    <p>图表1 生产 SQL 变更次数统计（申请人维度）</p>
    <div class="chart">
      <img src="{images['applicant_change_count']}" alt="生产 SQL 变更次数统计（申请人维度）">
    </div>

    <p>图表2 生产 SQL 变更次数统计（申请原因维度）</p>
    <div class="chart">
      <img src="{images['reason_category']}" alt="生产 SQL 变更次数统计（申请原因维度）">
    </div>

    <h3>慢服务分析</h3>
    <p>图表1 慢服务分析图表</p>
    <div class="chart">
      <img src="{images['slow_service']}" alt="慢服务分析图表">
    </div>

    <h3>数据库状态检查（慢 SQL）</h3>
    <p>图表1 慢 SQL 分析图表</p>
    <div class="chart">
      <img src="{images['slow_sql']}" alt="慢 SQL 分析图表">
    </div>

    <p>具体信息请查看附件。</p>

    <p class="note">ps. 生产数据变更线上台账 <a class="link" href="{ledger_url}">{ledger_url}</a></p>

    <h2>附件部分</h2>
    <ul>
      <li>生产数据变更</li>
      <li>慢服务分析 HTML 报告</li>
      <li>数据库状态检查</li>
    </ul>
  </div>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="生成周巡检邮件 HTML")
    parser.add_argument("period_dir", help="周巡检周期目录，例如 20260511-20260517")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="周巡检配置文件路径")
    args = parser.parse_args()

    config = load_config(args.config)
    period_dir = Path(args.period_dir)
    period = get_period(period_dir)

    images = {
        key: value.format(period=period)
        for key, value in config["images"].items()
    }
    for image in images.values():
        require_file(period_dir / image)

    output_path = period_dir / config.get("mail_html_name", "邮件内容.html")
    html = build_html(
        config.get("project_name", "贸易系统2.0项目"),
        period,
        config["ledger_url"],
        images,
    )
    output_path.write_text(html, encoding="utf-8")
    print(f"已生成邮件 HTML: {output_path}")


if __name__ == "__main__":
    main()
