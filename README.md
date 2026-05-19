# g3-inspection

贸易系统 2.0 **周巡检工作流自动化** Claude Code Skill。

通过三条指令完成从目录初始化、资产生成到邮件发送的全流程。

---

## 目录结构

```
g3-inspection/
├── SKILL.md                  # Skill 元数据与行为契约
├── agents/                   # 子 agent 定义
├── scripts/
│   ├── create_weekly_dir.sh  # 创建周巡检目录
│   ├── create_mail_html.py   # 生成邮件 HTML（内嵌图表）
│   ├── capture_slow_service_chart.py  # 截取慢服务图表 PNG
│   └── send_email.py         # 发送巡检邮件（支持 --dry-run）
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
20260511-20260517/
├── 慢服务报告/
├── 生产数据变更/
└── 数据库状态检查/
```

### 2. `week-generate` — 生成巡检资产

前置条件（需提前放入对应目录）：

- `生产数据变更/生产数据变更记录.xlsx`
- `慢服务报告/*.html`（慢服务报告，仅一份）
- `数据库状态检查/*.xlsx`（至少一份）

执行后生成：

- `生产数据变更/data_analysis_charts/` — 数据变更分析图表 PNG
- `慢服务报告/慢服务分析图表_{period}.png` — 慢服务图表截图
- `邮件内容.html` — 内嵌图表的邮件正文

### 3. `week-email` — 发送巡检邮件

**始终需要人工确认，不会自动发送。**

Skill 会先执行 dry-run，展示收件人、抄送、邮件主题供确认，用户明确同意后才发送。

---

## 邮件配置

复制模板并填入真实账号信息：

```bash
cp references/email-template.json references/email.json
# 编辑 email.json，填入 SMTP、账号密码、收件人等
```

`references/email.json` 已加入 `.gitignore`，不会提交到版本库。

---

## 依赖

- Python 3
- `playwright`（慢服务图表截图用）
- `openpyxl`、`matplotlib`（数据变更图表用）
- Claude Code（运行 Skill 的宿主环境）
