"""指标换版、影响预览与事实/口径差异分解。

核心规则：

* 新版必须先经影响预览 + 负责人确认才能成为 confirmed；
* 已定时的报告订阅锁定具体版本，不会悄悄改用新口径；
* 旧版文件保留，其他人仍可查询旧口径；
* 与上期比较时，把差异拆成「事实改变」与「口径改变」两段，可分别解释。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .context import DataContext
from .engine import Engine
from .errors import CheckError
from .measures import Query
from .registry import MetricDef, Registry

# 哪些字段变化属于"口径改变"（其余字段变化视为说明性修订）
DEFINITION_FIELDS = (
    "expression", "numerator", "denominator", "time_basis", "dedup_object",
    "eligible_dims", "additivity", "requires_attribution", "window_days",
)


@dataclass
class ImpactPreview:
    metric_code: str
    old_version: int
    new_version: int
    changed_fields: dict[str, tuple[Any, Any]]
    definition_change: bool
    affected_subscriptions: list[dict[str, Any]]
    sample_query: dict[str, Any]
    old_value: Any
    new_value: Any
    value_delta: Any
    blockers: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_code": self.metric_code,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "changed_fields": {k: {"from": v[0], "to": v[1]} for k, v in self.changed_fields.items()},
            "definition_change": self.definition_change,
            "affected_subscriptions": self.affected_subscriptions,
            "sample_query": self.sample_query,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "value_delta": self.value_delta,
            "blockers": self.blockers,
        }


class VersionService:
    def __init__(self, registry: Registry, state_dir: str | Path, engine: Engine):
        self.registry = registry
        self.state_dir = Path(state_dir)
        self.engine = engine
        self.preview_dir = self.state_dir / "impact_previews"
        self.preview_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 预览
    def preview_revision(
        self,
        code: str,
        changes: dict[str, Any],
        note: str,
        actor: str,
        ctx: DataContext,
        query_overrides: dict[str, Any] | None = None,
    ) -> ImpactPreview:
        old = self.registry.get(code)
        if old.status != "confirmed":
            raise CheckError("NOT_CONFIRMED", f"{code} 当前不是已确认版本，无法生成换版预览")

        # 在内存里构造候选新版本并做语义校核（不落盘，校核不过则拒绝换版）
        candidate_data = old.to_dict()
        candidate_data.update(changes)
        candidate_data.update({
            "version": old.version + 1, "status": "draft",
            "confirmed_at": None, "confirmed_by": None,
            "change_note": note,
        })
        candidate = MetricDef.from_dict(candidate_data)
        # 定义级硬错误（循环、时间归属冲突、维度资格、比率套比率）直接抛出，拒绝出预览
        from .checks import validate_definition
        validate_definition(self.registry, candidate)
        blockers: list[str] = []

        changed = {
            k: (old.to_dict().get(k), changes[k])
            for k in changes
            if old.to_dict().get(k) != changes[k] and k not in ("change_note",)
        }
        definition_change = any(k in DEFINITION_FIELDS for k in changed)

        # 影响面：哪些订阅锁定了该指标（按名称 + 当前版本绑定关系在 subscriptions 中登记）
        affected = self._affected_subscriptions(code)

        # 同一事实截面下新旧口径对照（隔离口径效应）
        query = Query(metric_code=code, **(query_overrides or {}))
        old_result = self.engine.run(query, ctx)
        old_value = _total_value(old_result)

        new_value: Any = None
        value_delta: Any = None
        if not blockers:
            # 预览语义：候选版本"视同已确认"参与求值，但不落盘、不改变登记册状态
            candidate.status = "confirmed"
            temp_reg = _RegistryView.overlay(self.registry, candidate)
            temp_engine = Engine(temp_reg)
            new_result = temp_engine.run(query, ctx)
            if new_result.blocked_reason:
                blockers.append(new_result.blocked_reason)
            else:
                new_value = _total_value(new_result)
                if old_value is not None and new_value is not None:
                    value_delta = new_value - old_value

        preview = ImpactPreview(
            metric_code=code,
            old_version=old.version,
            new_version=old.version + 1,
            changed_fields=changed,
            definition_change=definition_change,
            affected_subscriptions=affected,
            sample_query=query.__dict__.copy(),
            old_value=old_value,
            new_value=new_value,
            value_delta=value_delta,
            blockers=blockers,
        )
        path = self.preview_dir / f"{code}__v{old.version + 1}.json"
        path.write_text(json.dumps(preview.as_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return preview

    def confirm_revision(self, code: str, changes: dict[str, Any], note: str,
                         actor: str) -> MetricDef:
        """负责人确认后才真正写入新版本。"""
        draft = self.registry.revise(code, changes, note, actor)
        return self.registry.confirm(draft.code, actor)

    def _affected_subscriptions(self, code: str) -> list[dict[str, Any]]:
        sub_file = self.state_dir / "subscriptions.json"
        if not sub_file.exists():
            return []
        subs = json.loads(sub_file.read_text(encoding="utf-8"))
        metric = self.registry.get(code)
        return [
            {
                "subscription_id": s["id"],
                "recipient": s["recipient"],
                "locked_metric_version": s.get("metric_version"),
                "metric_name": s.get("metric_name", metric.name),
                "will_auto_upgrade": False,  # 永远不自动升级，必须收件方重新确认
            }
            for s in subs
            if s.get("metric_code") == code and s.get("status") in ("active", "renewal_required")
        ]

    # ------------------------------------------------------------ 差异分解
    def variance_bridge(
        self,
        code: str,
        version: int,
        current_ctx: DataContext,
        prior_ctx: DataContext,
        query_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构造事实/口径差异桥。

        四格：
          A=旧版口径 × 旧事实截面；B=旧版口径 × 新事实截面；C=新版口径 × 新事实截面。
          事实效应 = B-A（口径不动，只换事实）；口径效应 = C-B（事实不动，只换口径）。
        """
        history = {m.version: m for m in self.registry.history(code)}
        if version not in history:
            raise CheckError("VERSION_MISSING", f"{code} 不存在 v{version}")
        current = self.registry.get(code)

        query = Query(metric_code=code, **(query_overrides or {}))
        reg_a = _RegistryView.overlay(self.registry, history[version])
        a = _total_value(Engine(reg_a).run(query, prior_ctx))
        b = _total_value(Engine(reg_a).run(query, current_ctx))
        c = _total_value(self.engine.run(query, current_ctx))

        fact_effect = _sub(b, a)
        definition_effect = _sub(c, b)
        total = _sub(c, a)
        return {
            "metric_code": code,
            "comparison": {
                "A_old_definition_old_facts": a,
                "B_old_definition_new_facts": b,
                "C_new_definition_new_facts": c,
            },
            "fact_effect": fact_effect,
            "definition_effect": definition_effect,
            "total_delta": total,
            "reconciles": (
                fact_effect is not None and definition_effect is not None
                and abs((fact_effect + definition_effect) - (total or 0)) < 1e-12
            ),
            "prior_context": prior_ctx.describe(),
            "current_context": current_ctx.describe(),
        }


def _total_value(result) -> Any:
    rows = result.rows()
    if result.blocked_reason or not rows:
        return None
    # 总计查询只有一个 ALL 行
    return rows[0]["value"]


def _sub(a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    return a - b


class _RegistryView:
    """让临时候选版本参与引擎求值，而不污染正式登记册。"""

    @staticmethod
    def overlay(registry: Registry, candidate: MetricDef) -> Registry:
        view = registry.__class__.__new__(registry.__class__)
        view.state_dir = registry.state_dir
        view.version_dir = registry.version_dir
        view._defs = dict(registry._defs)  # type: ignore[attr-defined]
        view._defs[candidate.code] = candidate
        return view
