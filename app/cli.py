"""命令行入口：分析员与业务人员均可直接操作。

示例：
  python -m app.cli seed
  python -m app.cli catalog
  python -m app.cli query --metric sugar_documented_spec_ratio \
      --cutoff 2026-03-10T00:00:00+08:00 --start 2026-02-01 --end 2026-03-02
  python -m app.cli validate --metric repurchase_rate_30d
  python -m app.cli due --now 2026-04-07T10:00:00+08:00
  python -m app.cli inbox
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import CheckError, RegistryError
from .service import Application


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多粒度营养指标登记、查询与订阅应用")
    parser.add_argument("--state", default=None, help="本地状态目录（默认 local-state）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="写入内建指标目录并导入既有订阅约定")
    sub.add_parser("catalog", help="列出全部指标定义（含版本与状态）")

    p_hist = sub.add_parser("history", help="查看某指标的全部历史版本")
    p_hist.add_argument("--metric", required=True)

    p_reg = sub.add_parser("register", help="登记新指标（草稿），定义从 JSON 文件读取")
    p_reg.add_argument("--file", required=True)

    p_conf = sub.add_parser("confirm", help="校核并确认指标版本")
    p_conf.add_argument("--metric", required=True)
    p_conf.add_argument("--actor", required=True)

    p_val = sub.add_parser("validate", help="只校核不确认")
    p_val.add_argument("--metric", required=True)

    p_q = sub.add_parser("query", help="交互查询（业务人员）")
    p_q.add_argument("--metric", required=True)
    p_q.add_argument("--cutoff", required=True)
    p_q.add_argument("--start", required=True)
    p_q.add_argument("--end", required=True)
    p_q.add_argument("--mode", choices=["replay", "restate"], default="replay")
    p_q.add_argument("--region")
    p_q.add_argument("--family")
    p_q.add_argument("--spec")
    p_q.add_argument("--topic")
    p_q.add_argument("--group-by", default="", help="逗号分隔，如 region,topic")
    p_q.add_argument("--attribution", choices=["v1", "v2"])

    p_rev = sub.add_parser("revise", help="换版影响预览（不写新版本）")
    p_rev.add_argument("--metric", required=True)
    p_rev.add_argument("--file", required=True, help="changes JSON 文件")
    p_rev.add_argument("--note", required=True)
    p_rev.add_argument("--actor", required=True)
    p_rev.add_argument("--cutoff", required=True)
    p_rev.add_argument("--start", required=True)
    p_rev.add_argument("--end", required=True)

    p_crev = sub.add_parser("confirm-revision", help="负责人确认换版并通知订阅影响")
    p_crev.add_argument("--metric", required=True)
    p_crev.add_argument("--file", required=True)
    p_crev.add_argument("--note", required=True)
    p_crev.add_argument("--actor", required=True)

    p_renew = sub.add_parser("confirm-renewal", help="收件方重新确认订阅口径")
    p_renew.add_argument("--subscription", required=True)
    p_renew.add_argument("--actor", required=True)
    p_renew.add_argument("--version", type=int)

    p_due = sub.add_parser("due", help="执行到期出报")
    p_due.add_argument("--now", required=True)

    sub.add_parser("subscriptions", help="列出订阅及其状态")
    sub.add_parser("inbox", help="列出本地收件箱通知")

    p_bridge = sub.add_parser("bridge", help="事实/口径差异分解")
    p_bridge.add_argument("--metric", required=True)
    p_bridge.add_argument("--baseline-version", type=int, required=True)
    p_bridge.add_argument("--current-cutoff", required=True)
    p_bridge.add_argument("--prior-cutoff", required=True)
    p_bridge.add_argument("--start", required=True)
    p_bridge.add_argument("--end", required=True)
    p_bridge.add_argument("--current-mode", default="restate")
    p_bridge.add_argument("--prior-mode", default="replay")

    args = parser.parse_args(argv)
    app = Application(state_dir=args.state)

    try:
        if args.command == "seed":
            _print(app.seed())
        elif args.command == "catalog":
            _print(app.metric_catalog())
        elif args.command == "history":
            _print(app.history(args.metric))
        elif args.command == "register":
            _print(app.register_metric(json.loads(Path(args.file).read_text(encoding="utf-8"))))
        elif args.command == "confirm":
            _print(app.confirm_metric(args.metric, args.actor))
        elif args.command == "validate":
            from .checks import validate_definition
            validate_definition(app.registry, app.registry.get(args.metric))
            _print({"metric": args.metric, "validation": "passed"})
        elif args.command == "query":
            _print(app.query({
                "metric_code": args.metric,
                "cutoff": args.cutoff,
                "period_start": args.start,
                "period_end": args.end,
                "cutoff_mode": args.mode,
                "region": args.region,
                "family": args.family,
                "spec_id": args.spec,
                "topic": args.topic,
                "group_by": [g for g in args.group_by.split(",") if g],
                "attribution_version": args.attribution,
            }))
        elif args.command == "revise":
            _print(app.revise(
                args.metric,
                json.loads(Path(args.file).read_text(encoding="utf-8")),
                args.note, args.actor,
                {"cutoff": args.cutoff, "period_start": args.start,
                 "period_end": args.end},
            ))
        elif args.command == "confirm-revision":
            _print(app.confirm_revision(
                args.metric,
                json.loads(Path(args.file).read_text(encoding="utf-8")),
                args.note, args.actor,
            ))
        elif args.command == "confirm-renewal":
            _print(app.subscriptions.confirm_renewal(
                args.subscription, args.actor, args.version
            ).to_dict())
        elif args.command == "due":
            _print(app.subscriptions.run_due(args.now))
        elif args.command == "subscriptions":
            _print(app.subscriptions.list())
        elif args.command == "inbox":
            inbox = sorted((app.state_dir / "inbox").glob("*.json"))
            _print([json.loads(p.read_text(encoding="utf-8")) for p in inbox])
        elif args.command == "bridge":
            _print(app.variance_bridge({
                "metric_code": args.metric,
                "baseline_version": args.baseline_version,
                "current_cutoff": args.current_cutoff,
                "prior_cutoff": args.prior_cutoff,
                "prior_cutoff_mode": args.prior_mode,
                "current_cutoff_mode": args.current_mode,
                "period_start": args.start,
                "period_end": args.end,
            }))
        else:  # pragma: no cover
            parser.error("未知命令")
    except (CheckError, RegistryError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
