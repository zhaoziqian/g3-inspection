# g3-inspection

贸易系统 2.0 **周巡检工作流自动化** Claude Code Skill。

四条指令覆盖从目录初始化、数据库巡检、资产生成到邮件发送的全流程。

---

## 目录结构

```
g3-inspection/
├── SKILL.md                  # Skill 元数据与行为契约
├── agents/                   # 子 agent 定义
├── db/
│   ├── db_query              # db_query 可执行文件（macOS/Linux）
│   ├── db_query.exe          # db_query 可执行文件（Windows）
│   ├── config.json           # 数据库连接配置，含真实密码（已排除入库）
│   ├── config.example.json   # 连接配置模板（入库）
│   └── sql/                  # 巡检 SQL 文件（只读，禁止修改）
│       ├── 1-数据库容量.sql
│       ├── 2-表级容量 Top30.sql
│       ├── 3-连接数.sql
│       ├── 4-长时间 SQL.sql
│       ├── 5-锁等待.sql
│       ├── 6-表死元组\膨胀趋势.sql
│       ├── 7-索引使用情况.sql
│       └── 8-长事务.sql
├── scripts/
│   ├── create_weekly_dir.sh           # 创建周巡检目录
│   ├── create_mail_html.py            # 生成邮件 HTML（内嵌图表）
│   ├── capture_slow_service_chart.py  # 截取慢服务图表 PNG
│   ├── capture_sql_chart.py           # 截取慢 SQL 图表 PNG
│   └── send_email.py                  # 发送巡检邮件（支持 --dry-run）
└── references/
    ├── email-template.json   # 邮件配置模板（入库）
    └── email.json            # 正式邮件配置，含账号密码（已排除入库）
```

---

## 快速开始

在周巡检工作空间目录下与 Claude Code 对话，触发以下指令：

### 1. `week-init` — 初始化周目录

在当前目录创建本周（或指定周期）的巡检目录及固定子目录：

```
周巡检 week-init
```

生成结构：

```
20260601-20260607/
├── 慢服务报告/
├── 生产数据变更/
└── 数据库状态检查/
```

### 2. `week-db-inspect` — 数据库巡检

连接数据库执行全量巡检查询，生成结构化 Markdown 报告：

```
/g3-inspection week-db-inspect
```

输出位置：

```
20260601-20260607/数据库状态检查/数据库巡检_20260601-20260607.md
```

报告包含 8 个巡检维度（随 `db/sql/` 目录中的 SQL 文件动态增减）：

| 维度 | 说明 |
|------|------|
| 数据库容量 | 各库大小，与上周对比，磁盘用量估算 |
| 表级容量 Top30 | 按总大小排序，标注索引/数据比异常 |
| 连接数 | 按库/用户/状态汇总 |
| 长时间 SQL | 排查业务侧超时语句 |
| 锁等待 | 检查阻塞链 |
| 表死元组/膨胀趋势 | 检查 autovacuum 失控风险 |
| 索引使用情况 | 列出 idx_scan=0 的未使用索引 |
| 长事务 | 排查未提交的业务事务 |

自动检测操作系统，选择对应二进制（`db_query` 或 `db_query.exe`）。

### 3. `week-generate` — 生成巡检资产

前置条件（需提前放入对应目录）：

- `生产数据变更/生产数据变更记录.xlsx`
- `慢服务报告/*.html`（慢服务报告，仅一份）
- `数据库状态检查/*.html`（慢 SQL 报告，仅一份）
- `数据库状态检查/*.xlsx`（至少一份）

执行后生成：

- `生产数据变更/data_analysis_charts/` — 数据变更分析图表 PNG（2 张）
- `慢服务报告/慢服务分析图表_{period}.png` — 慢服务图表截图
- `数据库状态检查/慢SQL分析图表_{period}.png` — 慢 SQL 图表截图
- `邮件内容.html` — 内嵌图表的邮件正文（含 4 张内联图表）

### 4. `week-email` — 发送巡检邮件

**始终需要人工确认，不会自动发送。**

Skill 会先执行 dry-run，展示收件人、抄送、邮件主题供确认，用户明确同意后才发送。

邮件包含 **4 张内联图表**，以及以下附件：

| 附件 | 来源 | 发送文件名 |
|------|------|------------|
| 慢服务报告 HTML | `慢服务报告/*.html` | `慢服务报告_{period}.html` |
| 慢 SQL 报告 HTML | `数据库状态检查/*.html` | `慢SQL报告_{period}.html` |
| 生产数据变更 | `生产数据变更/` 目录 | `生产数据变更.zip` |
| 数据库状态检查 Excel | `数据库状态检查/*.xlsx` | 原文件名 |

---

## 配置

### 数据库连接

复制模板并填入真实连接信息：

```bash
cp db/config.example.json db/config.json
# 编辑 db/config.json，填入 host、user、password、database 等
```

`db/config.json` 已加入 `.gitignore`，不会提交到版本库。

### 邮件

复制模板并填入真实账号信息：

```bash
cp references/email-template.json references/email.json
# 编辑 references/email.json，填入 SMTP、账号密码、收件人等
```

`references/email.json` 已加入 `.gitignore`，不会提交到版本库。

---

## 依赖

- Python 3
- `openpyxl`、`matplotlib`（数据变更图表用）
- Google Chrome（headless 截图用，macOS 系统自带 `sips` 裁剪）
- Claude Code（运行 Skill 的宿主环境）
