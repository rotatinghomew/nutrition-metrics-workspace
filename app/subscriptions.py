"""订阅确认与到期出报。

订阅锁定三件套：经确认的指标版本、事实截止规则（replay/restate + cutoff）、
可比较维度。指标换版后已定时报告不会悄悄使用新口径，而是进入 ``renewal_required``
等待收件方重新确认；旧版仍可继续查询与出报，直到旧版所需资料被证实错误。

出报幂等：同一 (delivery_key, 期号) 重复执行只产生一份业务通知。
一个报告内的多个指标共用同一事实截面，杜绝"分子已更新、分母仍旧值"。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Any

from .context import DataContext, parse_dt
from .engine import Engine
from .errors import CheckError
from .registry import Registry


SUBSCRIPTION_STATUSES = ("active", "renewal_required", "suspended", "cancelled")

# 既有 report_agreements 约定名称 -> 登记册指标 code。
# 显式别名表，不做模糊匹配，避免把含义相近但口径不同的指标错配。
AGREEMENT_ALIASES = {
    "有糖资料规格占可售规格": "sugar_documented_spec_ratio",
    "有资料商品的成交金额份额": "documented_sales_amount_share",
}


@dataclass
class Subscription:
    id: str
    recipient: str
    metric_code: str
    metric_version: int
    metric_name: str
    attribution_version: str | None
    cutoff_mode: str                 # replay | restate
    cutoff: str
    period_start: str
    period_end: str
    timezone: str
    scheduled_at: str
    recurrence: str
    delivery_key: str
    status: str = "active"
    renewal_reason: str | None = None
    data_revision_notice: str | None = None
    comparable_dims: list[str] | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Subscription":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class SubscriptionService:
    def __init__(self, registry: Registry, engine: Engine, state_dir: str | Path,
                 data_root: str | Path):
        self.registry = registry
        self.engine = engine
        self.state_dir = Path(state_dir)
        self.data_root = Path(data_root)
        self.sub_file = self.state_dir / "subscriptions.json"
        self.inbox_dir = self.state_dir / "inbox"
        self.report_dir = self.state_dir / "reports"
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._subs: list[Subscription] = []
        self._load()

    # ------------------------------------------------------------ 持久化
    def _load(self) -> None:
        if self.sub_file.exists():
            self._subs = [Subscription.from_dict(r) for r in
                          json.loads(self.sub_file.read_text(encoding="utf-8"))]

    def _save(self) -> None:
        self.sub_file.write_text(
            json.dumps([s.to_dict() for s in self._subs], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self._subs]

    def get(self, sub_id: str) -> Subscription:
        for s in self._subs:
            if s.id == sub_id:
                return s
        raise CheckError("NO_SUBSCRIPTION", f"订阅 {sub_id} 不存在")

    # ------------------------------------------------------------ 登记/确认
    def create(self, data: dict[str, Any]) -> Subscription:
        metric = self.registry.get(data["metric_code"])
        if metric.status != "confirmed":
            raise CheckError("METRIC_NOT_CONFIRMED",
                             f"指标 {metric.code} 未确认，不能建立订阅")
        sub = Subscription(
            id=data["id"],
            recipient=data["recipient"],
            metric_code=metric.code,
            metric_version=metric.version,
            metric_name=metric.name,
            attribution_version=data.get("attribution_version"),
            cutoff_mode=data["cutoff_mode"],
            cutoff=data["cutoff"],
            period_start=data["period_start"],
            period_end=data["period_end"],
            timezone=data.get("timezone", "Asia/Shanghai"),
            scheduled_at=data["scheduled_at"],
            recurrence=data.get("recurrence", "monthly"),
            delivery_key=data["delivery_key"],
            comparable_dims=data.get("comparable_dims", metric.eligible_dims),
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        if any(s.id == sub.id or s.delivery_key == sub.delivery_key for s in self._subs):
            raise CheckError("DUP_SUBSCRIPTION", f"订阅 id 或 delivery_key 已存在: {sub.id}")
        self._subs.append(sub)
        self._save()
        return sub

    def import_agreements(self, agreements: list[dict[str, Any]]) -> list[Subscription]:
        """把 data/report_agreements.jsonl 的既有约定映射到指标版本。"""
        name_to_code = {m.name: m.code for m in self.registry.all()}
        imported = []
        for row in agreements:
            code = name_to_code.get(row["metric_name"]) or AGREEMENT_ALIASES.get(row["metric_name"])
            if code is None:
                continue  # 名称无法映射的约定留待人工处理，不静默猜测
            if not self.registry.has(code):
                continue
            if any(s.id == f"imp-{row['id']}" for s in self._subs):
                continue
            metric = self.registry.get(code)
            sub = Subscription(
                id=f"imp-{row['id']}",
                recipient=row["recipient"],
                metric_code=code,
                metric_version=metric.version,
                metric_name=metric.name,
                attribution_version=row["attribution_version"],
                cutoff_mode="replay",          # 原约定 cutoff 是固定时点，按复原语义
                cutoff=row["cutoff"],
                period_start=row["period_start"],
                period_end=row["period_end"],
                timezone=row["timezone"],
                scheduled_at=row["scheduled_at"],
                recurrence=row["recurrence"],
                delivery_key=row["delivery_key"],
                status="active" if row["confirmed"] else "renewal_required",
                comparable_dims=metric.eligible_dims,
            )
            self._subs.append(sub)
            imported.append(sub)
        self._save()
        return imported

    # ------------------------------------------------------------ 换版响应
    def mark_renewals(self, metric_code: str, reason: str) -> list[str]:
        """指标换版后，锁定旧版的订阅转入待重新确认（不影响其照发旧版的能力）。"""
        changed = []
        for s in self._subs:
            if s.metric_code == metric_code and s.status == "active":
                current = self.registry.get(metric_code)
                if s.metric_version < current.version:
                    s.status = "renewal_required"
                    s.renewal_reason = reason
                    changed.append(s.id)
        self._save()
        return changed

    def confirm_renewal(self, sub_id: str, actor: str,
                        target_version: int | None = None) -> Subscription:
        """收件方重新确认；可指定升级到新版本，或明确继续锁定旧版。"""
        sub = self.get(sub_id)
        metric = self.registry.get(sub.metric_code)
        target = target_version or metric.version
        versions = {m.version for m in self.registry.history(sub.metric_code)}
        if target not in versions:
            raise CheckError("VERSION_MISSING", f"{sub.metric_code} 无 v{target}")
        target_metric = next(m for m in self.registry.history(sub.metric_code)
                             if m.version == target)
        if target_metric.status not in ("confirmed", "deprecated"):
            raise CheckError("VERSION_NOT_CONFIRMED", f"v{target} 尚未确认")
        sub.metric_version = target
        sub.status = "active"
        sub.renewal_reason = None
        self._save()
        return sub

    def attach_data_revision_notice(self, sub_id: str, notice: str) -> None:
        """资料修订（区别于口径换版）：标注哪些订阅不能保证原含义。"""
        sub = self.get(sub_id)
        sub.data_revision_notice = notice
        self._save()

    # ------------------------------------------------------------ 到期出报
    def due_subscriptions(self, now: datetime | str) -> list[Subscription]:
        if isinstance(now, str):
            now = parse_dt(now)
        return [s for s in self._subs if parse_dt(s.scheduled_at) <= now
                and s.status in ("active", "renewal_required")]

    def run_due(self, now: datetime | str,
                extra_metrics: list[str] | None = None) -> list[dict[str, Any]]:
        """执行所有到期订阅。重复执行同一期不会产生第二份通知（幂等）。"""
        delivered = []
        for sub in self.due_subscriptions(now):
            period_key = f"{sub.period_start}_{sub.period_end}"
            notice_key = f"{sub.delivery_key}__{period_key}"
            notice_path = self.inbox_dir / f"{notice_key}.json"
            if notice_path.exists():
                delivered.append({"subscription_id": sub.id, "skipped": "duplicate_notice",
                                  "notice_key": notice_key})
                continue
            report = self._generate_report(sub, extra_metrics or [])
            notice = {
                "notice_key": notice_key,
                "recipient": sub.recipient,
                "subscription_id": sub.id,
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "status": sub.status,
                "renewal_reason": sub.renewal_reason,
                "data_revision_notice": sub.data_revision_notice,
                "report": report,
            }
            notice_path.write_text(json.dumps(notice, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            report_path = self.report_dir / f"{notice_key}.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            delivered.append({"subscription_id": sub.id, "skipped": None,
                              "notice_key": notice_key})
        return delivered

    def _generate_report(self, sub: Subscription, extra_metrics: list[str]) -> dict[str, Any]:
        # 锁定版本：即使登记册已有更新版，报告仍按订阅锁定的版本构造视图
        metric_history = {m.version: m for m in self.registry.history(sub.metric_code)}
        locked = metric_history.get(sub.metric_version)
        if locked is None:
            raise CheckError("VERSION_MISSING",
                             f"订阅 {sub.id} 锁定的 {sub.metric_code} v{sub.metric_version} 已不存在")
        if locked.status not in ("confirmed", "deprecated"):
            raise CheckError("VERSION_NOT_CONFIRMED", "锁定版本未确认，不能出报")

        # 整份报告唯一事实截面：所有指标共用，杜绝分子/分母截面不一致
        ctx = DataContext.build(
            mode=sub.cutoff_mode,
            cutoff=sub.cutoff,
            period_start=sub.period_start,
            period_end=sub.period_end,
            data_root=self.data_root,
        )
        from .versions import _RegistryView
        view = _RegistryView.overlay(self.registry, locked)
        engine = Engine(view)

        codes = [sub.metric_code] + [c for c in extra_metrics if c != sub.metric_code]
        sections = []
        for code in codes:
            target = view.get(code)
            if target.time_basis != locked.time_basis:
                sections.append({
                    "metric_code": code,
                    "blocked_reason": f"时间归属 {target.time_basis} 与报告主指标 "
                                      f"{locked.time_basis} 不一致，已拒绝混入同一报告",
                })
                continue
            result = engine.run(_QueryBuilder.report_query(sub, code), ctx)
            sections.append({
                "metric_code": code,
                "metric_version": target.version,
                "audit": result.audit(),
                "rows": result.rows(),
            })

        return {
            "delivery_key": sub.delivery_key,
            "period": {"start": sub.period_start, "end": sub.period_end},
            "fact_cross_section": ctx.describe(),
            "attribution_version": sub.attribution_version,
            "comparable_dims": sub.comparable_dims,
            "sections": sections,
        }


class _QueryBuilder:
    @staticmethod
    def report_query(sub: Subscription, metric_code: str):
        from .measures import Query
        return Query(metric_code=metric_code, attribution_version=sub.attribution_version)
