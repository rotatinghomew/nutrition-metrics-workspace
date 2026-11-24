"""应用装配：把登记册、引擎、版本服务、订阅服务组合成一个可操作入口。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .context import DataContext
from .engine import Engine
from .measures import Query
from .registry import Registry
from .subscriptions import SubscriptionService
from .versions import VersionService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = PROJECT_ROOT / "local-state"
SEED_CATALOG = Path(__file__).resolve().parent / "catalog" / "seed_metrics.json"


class Application:
    def __init__(self, state_dir: str | Path | None = None,
                 data_root: str | Path | None = None):
        self.state_dir = Path(state_dir or DEFAULT_STATE)
        self.data_root = Path(data_root or PROJECT_ROOT)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.registry = Registry(self.state_dir)
        self.engine = Engine(self.registry)
        self.versions = VersionService(self.registry, self.state_dir, self.engine)
        self.subscriptions = SubscriptionService(
            self.registry, self.engine, self.state_dir, self.data_root
        )

    # ------------------------------------------------------------ 初始化
    def seed(self, confirm: bool = True) -> dict[str, Any]:
        """首次写入内建指标目录；已存在的指标不重复登记。"""
        catalog = json.loads(SEED_CATALOG.read_text(encoding="utf-8"))
        created, confirmed, skipped = [], [], []
        for data in catalog:
            if self.registry.has(data["code"]):
                skipped.append(data["code"])
                continue
            metric = self.registry.register(data)
            created.append(metric.code)
            if confirm:
                self.registry.confirm(metric.code, "seed-owner")
                confirmed.append(metric.code)
        # 引入既有收件约定
        agreements = [
            json.loads(line)
            for line in (self.data_root / "data" / "report_agreements.jsonl")
            .read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        imported = self.subscriptions.import_agreements(agreements)
        return {
            "created": created, "confirmed": confirmed, "skipped_existing": skipped,
            "imported_agreements": [s.id for s in imported],
        }

    # ------------------------------------------------------------ 查询
    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        metric_code = payload["metric_code"]
        metric = self.registry.get(metric_code)
        ctx = DataContext.build(
            mode=payload.get("cutoff_mode", "replay"),
            cutoff=payload["cutoff"],
            period_start=payload["period_start"],
            period_end=payload["period_end"],
            data_root=self.data_root,
        )
        query = Query(
            metric_code=metric_code,
            region=payload.get("region"),
            family=payload.get("family"),
            spec_id=payload.get("spec_id"),
            topic=payload.get("topic"),
            group_by=payload.get("group_by", []),
            attribution_version=payload.get("attribution_version"),
        )
        result = self.engine.run(query, ctx)
        return {
            "audit": result.audit(),
            "rows": result.rows(),
            "blocked_reason": result.blocked_reason,
        }

    def metric_catalog(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.registry.all()]

    def history(self, code: str) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.registry.history(code)]

    # ------------------------------------------------------------ 登记/修订
    def register_metric(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.registry.register(data).to_dict()

    def edit_draft(self, code: str, changes: dict[str, Any]) -> dict[str, Any]:
        return self.registry.edit_draft(code, changes).to_dict()

    def confirm_metric(self, code: str, actor: str) -> dict[str, Any]:
        return self.registry.confirm(code, actor).to_dict()

    def revise(self, code: str, changes: dict[str, Any], note: str, actor: str,
               preview_ctx: dict[str, Any]) -> dict[str, Any]:
        ctx = DataContext.build(
            mode=preview_ctx.get("cutoff_mode", "replay"),
            cutoff=preview_ctx["cutoff"],
            period_start=preview_ctx["period_start"],
            period_end=preview_ctx["period_end"],
            data_root=self.data_root,
        )
        preview = self.versions.preview_revision(
            code, changes, note, actor, ctx,
            query_overrides=preview_ctx.get("query_overrides"),
        )
        return preview.as_dict()

    def confirm_revision(self, code: str, changes: dict[str, Any], note: str,
                         actor: str) -> dict[str, Any]:
        metric = self.versions.confirm_revision(code, changes, note, actor)
        reason = f"指标 {code} 已换版至 v{metric.version}：{note}"
        affected = self.subscriptions.mark_renewals(code, reason)
        return {"metric": metric.to_dict(), "renewal_required_subscriptions": affected}

    def variance_bridge(self, payload: dict[str, Any]) -> dict[str, Any]:
        current_ctx = DataContext.build(
            mode=payload["current_cutoff_mode"], cutoff=payload["current_cutoff"],
            period_start=payload["period_start"], period_end=payload["period_end"],
            data_root=self.data_root,
        )
        prior_ctx = DataContext.build(
            mode=payload["prior_cutoff_mode"], cutoff=payload["prior_cutoff"],
            period_start=payload["period_start"], period_end=payload["period_end"],
            data_root=self.data_root,
        )
        return self.versions.variance_bridge(
            payload["metric_code"], payload["baseline_version"],
            current_ctx, prior_ctx, payload.get("query_overrides"),
        )
