---
name: g3-inspection
description: >
  Trade System 2.0 weekly inspection workflow automation. Use when the user mentions
  g3 inspection, 贸易系统2.0巡检, 周巡检, week-init, week-generate, week-email,
  creating weekly inspection directories, generating weekly inspection email HTML,
  generating inspection email assets, or sending weekly inspection emails with inline
  images and attachments.
---

# G3 Inspection

Use this skill to operate the Trade System 2.0 weekly inspection workflow.

## Supported Commands

- `week-init`: create or complete the weekly inspection directory.
- `week-generate`: generate inspection assets and `邮件内容.html`.
- `week-email`: send the weekly inspection email.
- `week-generate + week-email`: run generation first, then prepare email sending. Still require user confirmation before sending.

## Workspace Contract

Work in the user's current inspection workspace. If the workspace already has `AGENTS.md`, `config/`, and `scripts/`, prefer those local files. If required files are missing, copy bundled resources from this skill:

- `scripts/create_weekly_dir.sh`
- `scripts/capture_slow_service_chart.py`
- `scripts/create_mail_html.py`
- `scripts/send_email.py`
- `references/weekly_inspection.json` -> `config/weekly_inspection.json`
- `references/email.json` -> `config/email.json`

Weekly directory format:

```text
YYYYMMDD-YYYYMMDD/
├── 慢服务报告/
├── 生产数据变更/
└── 数据库状态检查/
```

The period is Monday through Sunday. If no period is specified, use the most recent completed Monday-Sunday week.

## week-init

Run:

```bash
scripts/create_weekly_dir.sh
```

Or with an explicit period:

```bash
scripts/create_weekly_dir.sh 20260511 20260517
```

The script must only create or complete missing fixed subdirectories. It must not overwrite existing user materials.

## week-generate

Before generating, identify the period directory. Confirm these source inputs exist:

- `生产数据变更/生产数据变更记录.xlsx`
- exactly one slow-service report HTML under `慢服务报告/`
- at least one Excel file under `数据库状态检查/`

Generate production data change charts by using `$g3-data-change-charts` against `生产数据变更/` with the period start and end dates:

- chart 1: applicant change count.
- chart 4: reason category.

Expected outputs:

```text
生产数据变更/data_analysis_charts/各申请人变更次数_{period}.png
生产数据变更/data_analysis_charts/申请原因分类_{period}.png
```

Generate one slow-service chart PNG from the slow-service HTML and place it under `慢服务报告/`:

```text
慢服务报告/慢服务分析图表_{period}.png
```

Use a local workspace method when present. Otherwise copy and run the bundled script:

```bash
scripts/capture_slow_service_chart.py <period-dir>
```

This renders the HTML with headless Chrome and crops the main chart region. Verify the PNG exists and is not blank.

Generate email HTML:

```bash
scripts/create_mail_html.py <period-dir>
```

Validate:

```bash
scripts/send_email.py --html <period-dir>/邮件内容.html --dry-run
```

The dry run must report 3 inline images and the expected attachments.

## week-email

Never send immediately.

First run a dry run:

```bash
scripts/send_email.py --html <period-dir>/邮件内容.html --dry-run
```

Then show the user these confirmation fields:

- 收件人
- 抄送人
- 邮件主题

Ask the user to confirm. Only after explicit user confirmation, send:

```bash
scripts/send_email.py --html <period-dir>/邮件内容.html
```

If the user requests `week-generate + week-email`, complete `week-generate`, run dry-run, show the confirmation fields, and stop until the user confirms.

## Email Rules

`config/email.json` controls SMTP, recipients, cc, subject template, and attachment rules.

The subject should use `{period}`:

```json
"subject_template": "贸易系统2.0项目{period}周巡检报告"
```

The send script embeds local `<img>` files as CID inline images so recipients can see charts.

Weekly attachments are fixed:

- the only HTML under `慢服务报告/`, sent as `慢服务报告_{period}.html`;
- the entire `生产数据变更/` directory, zipped as `生产数据变更.zip`;
- Excel files under `数据库状态检查/`, sent directly without zipping the directory.

## Safety

- Do not modify `归档/` unless the user explicitly approves it.
- Do not invent inspection source data.
- Do not overwrite user-provided files.
- Do not send email without the confirmation step.
- Keep git focused on reusable workflow assets, not weekly inspection contents.
