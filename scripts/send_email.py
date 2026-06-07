#!/usr/bin/env python3
import argparse
import html
import json
import mimetypes
import os
import shutil
import smtplib
import ssl
import sys
import tempfile
import uuid
import zipfile
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "references" / "email.json"


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"缺少环境变量: {name}")
    return value


def get_password(account_config):
    password = account_config.get("password")
    if password:
        return password
    return require_env(account_config.get("password_env", "SMTP_PASSWORD"))


def get_period(html_path):
    for part in reversed(html_path.parts):
        if len(part) == 17 and part[8] == "-":
            left, right = part.split("-", 1)
            if left.isdigit() and right.isdigit() and len(left) == 8 and len(right) == 8:
                return part
    return ""


def get_subject(message_config, period):
    subject_template = message_config.get("subject_template")
    if subject_template:
        return subject_template.format(period=period)
    subject = message_config.get("subject")
    if subject:
        return subject
    if period:
        return f"贸易系统2.0项目{period}周巡检报告"
    return "贸易系统2.0周巡检报告"


class ImageSrcParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.srcs = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "img":
            return
        for name, value in attrs:
            if name.lower() == "src" and value:
                self.srcs.append(value)


def is_local_src(src):
    lower = src.lower()
    return not (
        lower.startswith("http://")
        or lower.startswith("https://")
        or lower.startswith("cid:")
        or lower.startswith("data:")
    )


def collect_local_images(html_body, base_dir):
    parser = ImageSrcParser()
    parser.feed(html_body)

    images = []
    seen = set()
    for src in parser.srcs:
        if src in seen or not is_local_src(src):
            continue
        seen.add(src)
        image_path = (base_dir / src).resolve()
        if not image_path.is_file():
            raise SystemExit(f"HTML 图片不存在，无法内嵌: {src}")
        cid = f"img-{uuid.uuid4().hex}@weekly-inspection"
        images.append((src, image_path, cid))
    return images


def replace_image_srcs(html_body, images):
    updated = html_body
    for src, _image_path, cid in images:
        escaped_src = html.escape(src, quote=True)
        updated = updated.replace(f'src="{escaped_src}"', f'src="cid:{cid}"')
        updated = updated.replace(f"src='{escaped_src}'", f"src='cid:{cid}'")
        updated = updated.replace(f'src="{src}"', f'src="cid:{cid}"')
        updated = updated.replace(f"src='{src}'", f"src='cid:{cid}'")
    return updated


def attach_inline_images(html_part, images):
    for _src, image_path, cid in images:
        mime_type, _encoding = mimetypes.guess_type(image_path.name)
        if not mime_type:
            mime_type = "image/png"
        maintype, subtype = mime_type.split("/", 1)
        html_part.add_related(
            image_path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            cid=f"<{cid}>",
            filename=image_path.name,
        )


def resolve_attachment(path_value, base_dir):
    path = Path(path_value)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def format_name(value, period):
    if not value:
        return value
    return value.format(period=period)


def add_file_attachment(msg, file_path, filename=None):
    attachment_name = filename or file_path.name
    mime_type, _encoding = mimetypes.guess_type(attachment_name)
    if not mime_type:
        mime_type = "application/octet-stream"
    maintype, subtype = mime_type.split("/", 1)
    msg.add_attachment(
        file_path.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=attachment_name,
    )


def expand_attachment_paths(path_value, html_base_dir):
    path_text = str(path_value)
    path = Path(path_text)
    if path.is_absolute():
        matches = sorted(Path("/").glob(path_text.lstrip("/"))) if any(ch in path_text for ch in "*?[]") else [path]
    else:
        matches = sorted(html_base_dir.glob(path_text)) if any(ch in path_text for ch in "*?[]") else [html_base_dir / path]
    return [match.resolve() for match in matches]


def normalize_attachment_spec(value, period):
    if isinstance(value, str):
        return {
            "path": format_name(value, period),
        }
    if not isinstance(value, dict):
        raise SystemExit(f"附件配置格式错误: {value}")
    spec = dict(value)
    spec["path"] = format_name(spec.get("path"), period)
    spec["filename"] = format_name(spec.get("filename"), period)
    return spec


def archive_html_attachment_specs(values):
    specs = []
    for value in values:
        spec = {"path": value} if isinstance(value, str) else dict(value)
        if str(spec.get("path", "")).lower().endswith(".html"):
            spec["archive"] = True
            filename = spec.get("filename")
            if filename:
                spec["filename"] = str(Path(filename).with_suffix(".zip"))
        specs.append(spec)
    return specs


def add_attachments(msg, attachment_values, html_base_dir, period):
    temp_dirs = []
    attached = []
    for value in attachment_values:
        spec = normalize_attachment_spec(value, period)
        path_value = spec["path"]
        paths = expand_attachment_paths(path_value, html_base_dir)
        paths = [path for path in paths if path.exists()]

        if not paths:
            raise SystemExit(f"附件路径不存在: {path_value}")
        if len(paths) > 1 and spec.get("filename"):
            raise SystemExit(f"附件配置 {path_value} 匹配多个文件，不能使用单个 filename")

        for path in paths:
            if path.is_dir() and spec.get("archive") is False:
                raise SystemExit(f"附件路径是目录，必须启用 archive: {path_value}")
            if path.is_dir() or spec.get("archive") is True:
                temp_dir = tempfile.mkdtemp(prefix="weekly-mail-")
                temp_dirs.append(temp_dir)
                filename = spec.get("filename") or f"{path.name}.zip"
                zip_stem = Path(filename).stem
                zip_base = Path(temp_dir) / zip_stem
                if path.is_dir():
                    zip_path = Path(shutil.make_archive(str(zip_base), "zip", path))
                else:
                    zip_path = zip_base.with_suffix(".zip")
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                        archive.write(path, arcname=path.name)
                add_file_attachment(msg, zip_path, filename=filename)
                attached.append(filename)
                continue

            filename = spec.get("filename") or path.name
            add_file_attachment(msg, path, filename=filename)
            attached.append(filename)

    return attached, temp_dirs


def main():
    parser = argparse.ArgumentParser(description="发送周巡检 HTML 邮件")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="邮件配置文件路径")
    parser.add_argument("--html", required=True, help="邮件 HTML 正文文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只检查邮件内容和附件，不实际发送")
    parser.add_argument("--timeout", type=int, default=120, help="SMTP 连接和写入超时秒数")
    parser.add_argument("--to", action="append", dest="mail_to", help="覆盖收件人，可重复指定")
    parser.add_argument("--no-cc", action="store_true", help="不发送抄送")
    parser.add_argument("--archive-html-attachments", action="store_true", help="将 HTML 附件压缩为 ZIP")
    args = parser.parse_args()

    config = load_config(args.config)
    smtp_config = config["smtp"]
    account_config = config["account"]
    message_config = config["message"]

    smtp_host = smtp_config.get("host", "smtp.chinalco.com.cn")
    smtp_port = int(smtp_config.get("port", 465))
    smtp_ssl = bool(smtp_config.get("ssl", True))
    ssl_verify = bool(smtp_config.get("ssl_verify", True))

    smtp_user = account_config["user"]
    smtp_password = get_password(account_config)

    mail_to = args.mail_to or as_list(message_config.get("to"))
    mail_cc = [] if args.no_cc else as_list(message_config.get("cc"))
    if not mail_to:
        raise SystemExit("配置文件中 message.to 不能为空")

    html_path = Path(args.html)
    html_body = html_path.read_text(encoding="utf-8")
    html_base_dir = html_path.parent
    period = get_period(html_path)
    images = collect_local_images(html_body, html_base_dir)
    html_body = replace_image_srcs(html_body, images)

    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = ", ".join(mail_to)
    if mail_cc:
        msg["Cc"] = ", ".join(mail_cc)
    msg["Subject"] = get_subject(message_config, period)
    msg.set_content("请使用支持 HTML 的邮件客户端查看本邮件。")
    msg.add_alternative(html_body, subtype="html")
    html_part = msg.get_payload()[-1]
    attach_inline_images(html_part, images)
    attachment_values = as_list(message_config.get("attachments"))
    if args.archive_html_attachments:
        attachment_values = archive_html_attachment_specs(attachment_values)
    attached_files, temp_dirs = add_attachments(
        msg,
        attachment_values,
        html_base_dir,
        period,
    )

    recipients = mail_to + mail_cc
    try:
        if args.dry_run:
            print("DRY_RUN: 邮件检查通过，未发送")
            print(f"主题: {msg['Subject']}")
            print(f"收件人: {', '.join(mail_to)}")
            if mail_cc:
                print(f"抄送: {', '.join(mail_cc)}")
            print(f"内嵌图片: {len(images)} 张")
            if attached_files:
                print(f"附件: {', '.join(attached_files)}")
            return

        if smtp_ssl:
            context = ssl.create_default_context() if ssl_verify else ssl._create_unverified_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=args.timeout) as smtp:
                smtp.login(smtp_user, smtp_password)
                smtp.send_message(msg, to_addrs=recipients)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=args.timeout) as smtp:
                smtp.login(smtp_user, smtp_password)
                smtp.send_message(msg, to_addrs=recipients)

        print(f"邮件已发送: {smtp_user} -> {', '.join(recipients)}")
        print(f"内嵌图片: {len(images)} 张")
        if attached_files:
            print(f"附件: {', '.join(attached_files)}")
    finally:
        for temp_dir in temp_dirs:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"发送失败: {exc}", file=sys.stderr)
        raise
