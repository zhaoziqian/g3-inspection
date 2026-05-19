#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUB_DIRS=("慢服务报告" "生产数据变更" "数据库状态检查")

usage() {
  cat <<'USAGE'
用法:
  scripts/create_weekly_dir.sh
  scripts/create_weekly_dir.sh <开始日期> <结束日期>

说明:
  - 无参数时，创建最近一个已结束的周巡检目录。
  - 周巡检目录统一创建在当前工作空间根目录下。
  - 周期目录下固定包含 慢服务报告、生产数据变更、数据库状态检查 三个子目录。
  - 周巡检周期固定为周一至周日。
  - 日期格式支持 YYYYMMDD 或 YYYY-MM-DD。
USAGE
}

ensure_sub_dirs() {
  local dir="$1"
  local sub_dir

  for sub_dir in "${SUB_DIRS[@]}"; do
    mkdir -p "${dir}/${sub_dir}"
  done
}

normalize_date() {
  local value="$1"

  if [[ "$value" =~ ^[0-9]{8}$ ]]; then
    printf '%s' "$value"
    return
  fi

  if [[ "$value" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    printf '%s' "${value//-/}"
    return
  fi

  echo "日期格式错误: $value" >&2
  usage >&2
  exit 1
}

validate_range() {
  local start="$1"
  local end="$2"

  python3 - "$start" "$end" <<'PY'
import datetime
import sys

start = datetime.datetime.strptime(sys.argv[1], "%Y%m%d").date()
end = datetime.datetime.strptime(sys.argv[2], "%Y%m%d").date()

if start > end:
    raise SystemExit("开始日期不能晚于结束日期")

if start.weekday() != 0 or end.weekday() != 6:
    raise SystemExit("周巡检周期必须为周一至周日")

if (end - start).days != 6:
    raise SystemExit("周巡检周期必须正好为 7 天")
PY
}

recent_range() {
  python3 <<'PY'
import datetime

today = datetime.date.today()
this_monday = today - datetime.timedelta(days=today.weekday())
start = this_monday - datetime.timedelta(days=7)
end = this_monday - datetime.timedelta(days=1)

print(f"{start:%Y%m%d} {end:%Y%m%d}")
PY
}

if [[ $# -eq 0 ]]; then
  read -r start_date end_date < <(recent_range)
elif [[ $# -eq 2 ]]; then
  start_date="$(normalize_date "$1")"
  end_date="$(normalize_date "$2")"
else
  usage >&2
  exit 1
fi

validate_range "$start_date" "$end_date"

target_dir="${ROOT_DIR}/${start_date}-${end_date}"

if [[ -d "$target_dir" ]]; then
  ensure_sub_dirs "$target_dir"
  printf '周巡检目录已存在，已补齐固定子目录，未覆盖已有内容: %s\n' "$target_dir"
  exit 0
fi

if [[ -e "$target_dir" ]]; then
  printf '同名路径已存在但不是目录，未处理: %s\n' "$target_dir" >&2
  exit 1
fi

mkdir "$target_dir"
ensure_sub_dirs "$target_dir"
printf '已创建周巡检目录: %s\n' "$target_dir"
