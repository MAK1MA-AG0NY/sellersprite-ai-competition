"""Small, explicit CLI; no background tasks or mail access on import."""
from __future__ import annotations

import argparse
import json
import platform
import sys

from . import __version__, parser as reply_parser, repository as repo
from .config import ConfigError, load_settings
from .csv_import import import_csv
from .service import (
    export_report, fetch_inbox, preview_query, reconcile_query, reprocess_incoming, review_event,
    run_alerts, send_next_query, test_mail_auth,
)


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m feta", description="FedEx Email Tracking Automation (experimental)")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="离线检查配置，不连接邮箱")
    imp = commands.add_parser("import", help="导入 UTF-8 FedEx CSV")
    imp.add_argument("csv_path")

    query = commands.add_parser("query", help="预览或发送 FedEx 邮件查询")
    query_sub = query.add_subparsers(dest="query_command", required=True)
    query_sub.add_parser("preview", help="显示分批和纯跟踪号正文，不发送")
    send = query_sub.add_parser("send", help="最多发送一批；需要显式确认并关闭 DRY_RUN")
    send.add_argument("--confirm-send", action="store_true")
    reconcile = query_sub.add_parser("reconcile", help="人工核对 SMTP 结果不确定的批次")
    reconcile.add_argument("batch_id")
    reconcile.add_argument("--status", choices=("submitted", "failed"), required=True)

    mail = commands.add_parser("mail", help="邮箱账号操作")
    mail_sub = mail.add_subparsers(dest="mail_command", required=True)
    auth = mail_sub.add_parser("test-auth", help="显式测试 SMTP/IMAP 认证，不发送邮件")
    auth.add_argument("--confirm-network", action="store_true")
    mail_sub.add_parser("disable", help="停用本地 FETA 邮箱账号")

    inbox = commands.add_parser("inbox", help="收取并复核 FedEx 邮件")
    inbox_sub = inbox.add_subparsers(dest="inbox_command", required=True)
    fetch = inbox_sub.add_parser("fetch", help="只读拉取未读邮件，需要显式网络确认")
    fetch.add_argument("--confirm-network", action="store_true")
    reprocess = inbox_sub.add_parser("reprocess", help="重新验证未认证邮件；按 UID 从邮箱只读重取")
    reprocess.add_argument("message_id", type=int)
    reprocess.add_argument("--confirm-network", action="store_true")
    inbox_sub.add_parser("messages", help="列出已处理邮件摘要")
    detail = inbox_sub.add_parser("show", help="查看不含原文的邮件解析摘要")
    detail.add_argument("message_id", type=int)
    inbox_sub.add_parser("pending", help="列出待人工复核物流事件")
    review = inbox_sub.add_parser("review", help="复核实验解析事件")
    review.add_argument("event_id", type=int)
    review.add_argument("--decision", choices=("accept", "ignore"), required=True)

    alerts = commands.add_parser("alerts", help="状态异常告警去重")
    alerts_sub = alerts.add_subparsers(dest="alerts_command", required=True)
    alert_run = alerts_sub.add_parser("run", help="计算本地新告警；可选发送给运营邮箱")
    alert_run.add_argument("--send", action="store_true")
    alert_run.add_argument("--confirm-send", action="store_true")
    ack = alerts_sub.add_parser("set", help="更新告警状态；设为 open 会人工重新排队邮件")
    ack.add_argument("alert_id", type=int)
    ack.add_argument("--state", choices=("open", "acknowledged", "ignored"), required=True)
    alerts_sub.add_parser("list", help="列出未关闭告警")

    report = commands.add_parser("report", help="本地报表")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    export = report_sub.add_parser("export", help="导出 CSV")
    export.add_argument("--output", required=True)

    shipments = commands.add_parser("shipments", help="管理本地查询暂停项")
    ship_sub = shipments.add_subparsers(dest="ship_command", required=True)
    resume = ship_sub.add_parser("resume", help="人工重新启用暂停/关闭的跟踪号")
    resume.add_argument("tracking_number")
    return root


def _doctor(settings) -> int:
    repo.init_db(settings.db_path)
    print(f"FETA {__version__} | Python {platform.python_version()}")
    print(f"SQLite 初始化：ok | path={settings.db_path}")
    print(f"SMTP 配置：{'已配置' if settings.smtp_configured else '未配置'} | IMAP 配置：{'已配置' if settings.imap_configured else '未配置'}")
    print(f"DRY_RUN={str(settings.dry_run).lower()} | 每轮上限=30 | query interval={settings.query_interval_hours:g}h")
    print(f"解析器={reply_parser.PARSER_VERSION} | FedEx真实回信样本=未验证 | 邮件原文归档=关闭")
    print("离线检查未连接网络、未发送邮件；真实 SMTP/IMAP 需使用单独的确认参数。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
        if args.command == "doctor":
            return _doctor(settings)
        if args.command == "import":
            result = import_csv(args.csv_path, settings.db_path)
            print(f"导入完成：新增运单 {result['added_shipments']}，重复行 {result['duplicate_rows']}，订单关联 {result['order_links_added']}")
            return 0
        if args.command == "query":
            if args.query_command == "preview":
                batches = preview_query(db_path=settings.db_path)
                if not batches:
                    print("没有到期的 FedEx 运单。")
                    return 0
                for index, batch in enumerate(batches, 1):
                    print(f"批次 {index} | 数量 {batch['count']} / 30")
                    print("To: track@fedex.com")
                    print("Subject: [空白]")
                    print("正文（每行一个跟踪号）：")
                    print(batch["body"])
                print("预览完成；没有创建批次，也没有发送邮件。")
                return 0
            if args.query_command == "send":
                if not args.confirm_send:
                    raise PermissionError("缺少 --confirm-send；未连接 SMTP")
                result = send_next_query(settings)
                print(f"查询结果：{result['status']} | 批次={result['batch_id'] or '-'} | 数量={result['count']}")
                return 0 if result["status"] in {"submitted", "cooldown", "no_due_shipments"} else 2
            result = reconcile_query(args.batch_id, args.status, settings)
            print(f"批次 {result['id']} 已人工记录为 {result['send_status']}。")
            return 0
        if args.command == "mail":
            if args.mail_command == "disable":
                if not settings.mail_user or "@" not in settings.mail_user:
                    raise ConfigError("请先在 .env.feta 配置 MAIL_USER，再停用邮箱账号")
                repo.register_mail_account(settings.mail_user, settings.mail_provider, db_path=settings.db_path)
                repo.set_mail_account_state("disabled", db_path=settings.db_path)
                print("本地邮箱账号已停用；没有连接邮箱。")
                return 0
            if not args.confirm_network:
                raise PermissionError("缺少 --confirm-network；未连接 SMTP/IMAP")
            result = test_mail_auth(settings)
            print(f"SMTP={result['smtp']} | IMAP={result['imap']} | state={result['account_state']}")
            return 0
        if args.command == "inbox":
            if args.inbox_command == "review":
                result = review_event(args.event_id, args.decision, settings)
                print(f"物流事件 {result['event_id']}：{result['review_state']} | 运单 {result['tracking_number']}")
                return 0
            if args.inbox_command == "reprocess":
                if not args.confirm_network:
                    raise PermissionError("缺少 --confirm-network；未连接 IMAP")
                result = reprocess_incoming(settings, args.message_id)
                print("重处理结果：" + " | ".join(f"{key}={value}" for key, value in result.items()))
                return 0
            if args.inbox_command == "pending":
                items = repo.review_events(settings.db_path)
                if not items:
                    print("没有待复核事件。")
                for item in items:
                    print(f"事件 #{item['id']} | {item['tracking_number']} | {item['normalized_status']} | "
                          f"{item['raw_status'] or '未知状态'} | ETA={item['raw_eta_text'] or '-'}")
                return 0
            if args.inbox_command == "messages":
                items = repo.list_incoming_messages(db_path=settings.db_path)
                if not items:
                    print("没有已处理邮件摘要。")
                for item in items:
                    print(f"邮件摘要 #{item['id']} | {item['received_at_utc']} | {item['reply_status']} | "
                          f"authenticated={bool(item['auth_verified'])} | UID={item['uidvalidity']}/{item['uid']}")
                return 0
            if args.inbox_command == "show":
                print(json.dumps(repo.incoming_message_detail(args.message_id, settings.db_path),
                                 ensure_ascii=False, indent=2))
                return 0
            if not args.confirm_network:
                raise PermissionError("缺少 --confirm-network；未连接 IMAP")
            result = fetch_inbox(settings)
            print("收件完成：" + " | ".join(f"{key}={value}" for key, value in result.items()))
            return 0
        if args.command == "alerts":
            if args.alerts_command == "list":
                items = repo.list_alerts(settings.db_path)
                if not items:
                    print("没有未关闭告警。")
                for item in items:
                    print(f"#{item['id']} {item['severity']} {item['rule_code']} {item['tracking_number']} "
                          f"[{item['state']} / email={item['notification_status']}]")
                return 0
            if args.alerts_command == "set":
                repo.set_alert_state(args.alert_id, args.state, db_path=settings.db_path)
                print(f"告警 {args.alert_id} 已设为 {args.state}。")
                return 0
            if args.send and not args.confirm_send:
                raise PermissionError("发送告警还需 --confirm-send；未连接 SMTP")
            result = run_alerts(settings, send=args.send)
            print(f"新告警={len(result['new_alerts'])} | 待发送={result['pending_notifications']} | "
                  f"已发送={result['sent']} | 明确失败={result['failed']} | 结果不确定={result['unknown']}")
            for item in result["new_alerts"]:
                print(f"#{item['id']} {item['severity']} {item['rule_code']} {item['tracking_number']}")
            return 0
        if args.command == "report":
            count = export_report(args.output, settings.db_path)
            print(f"CSV 已导出：{args.output} | 行数={count}")
            return 0
        if args.command == "shipments":
            count = repo.enable_shipment(args.tracking_number, db_path=settings.db_path)
            if not count:
                raise ValueError("本地未找到该跟踪号")
            print("运单已重新启用；此操作不会发送邮件。")
            return 0
    except (ConfigError, PermissionError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Avoid printing exception text from mail libraries, which can contain addresses or server details.
        print(f"操作失败：{type(exc).__name__}", file=sys.stderr)
        return 2
    return 2
