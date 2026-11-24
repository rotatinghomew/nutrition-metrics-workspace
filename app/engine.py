"""查询引擎：把已确认的指标定义在事实截面上求值。

输出的每个数字都带：值、不确定区间值（缺项不当零）、口径说明、归属版本、
事实截止规则、去重对象与分母分子来源，供业务端打开数值即可审计。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import dsl
from .errors import CheckError
from .checks import validate_query
from .context import DataContext
from .measures import BASE_MEASURES, DIM_KEYS, GroupResult, Query, TOTAL_KEY
from .registry import MetricDef, Registry


@dataclass
class MetricResult:
    metric: MetricDef
    groups: dict[tuple, GroupResult]
    context: DataContext
    blocked_reason: str | None = None
    lineage: list[str] = field(default_factory=list)

    def rows(self) -> list[dict[str, Any]]:
        out = []
        for key, result in sorted(self.groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
            row = {dim: (value if value != TOTAL_KEY else None)
                   for dim, value in zip(DIM_KEYS, key)}
            row.update(result.as_dict())
            out.append(row)
        return out

    def audit(self) -> dict[str, Any]:
        m = self.metric
        return {
            "metric_code": m.code,
            "metric_name": m.name,
            "version": m.version,
            "status": m.status,
            "owner": m.owner,
            "unit": m.unit,
            "time_basis": m.time_basis,
            "dedup_object": m.dedup_object,
            "numerator": m.numerator,
            "denominator": m.denominator,
            "additivity": m.additivity,
            "requires_attribution": m.requires_attribution,
            "window_days": m.window_days,
            "expression": m.expression,
            "lineage": self.lineage,
            "fact_context": self.context.describe(),
            "blocked_reason": self.blocked_reason,
        }


class Engine:
    def __init__(self, registry: Registry):
        self.registry = registry

    def run(self, query: Query, ctx: DataContext, _stack: list[str] | None = None) -> MetricResult:
        metric = self.registry.get(query.metric_code)
        decision = validate_query(self.registry, metric, query)
        if not decision.ok:
            return MetricResult(metric=metric, groups={}, context=ctx,
                                blocked_reason=decision.reason)

        stack = _stack or []
        if metric.code in stack:
            raise CheckError("CYCLE", f"循环依赖: {' -> '.join(stack + [metric.code])}")
        stack = stack + [metric.code]

        if metric.kind == "base":
            groups = self._run_base(metric, query, ctx)
            lineage = [f"基础度量 {metric.code} 在安全粒度直接计算"]
        elif metric.kind == "cohort_window":
            groups = self._run_cohort(metric, query, ctx)
            lineage = [f"窗口指标 {metric.code}：window_days={metric.window_days}，"
                       "仅纳入观察窗已走完的首购家庭，删重票据不删真实二次购买"]
        else:
            groups, lineage = self._run_derived(metric, query, ctx, stack)

        return MetricResult(metric=metric, groups=groups, context=ctx, lineage=lineage)

    # ------------------------------------------------------------ 基础
    def _run_base(self, metric: MetricDef, query: Query, ctx: DataContext) -> dict[tuple, GroupResult]:
        func = BASE_MEASURES[metric.code]
        if metric.code == "repurchase_cohort":
            query.window_days = metric.window_days
        return func(ctx, query)

    # ------------------------------------------------------------ 派生
    def _run_derived(self, metric: MetricDef, query: Query, ctx: DataContext,
                     stack: list[str]) -> tuple[dict[tuple, GroupResult], list[str]]:
        ast = dsl.parse(metric.expression or "")
        refs = sorted(ast.references())

        # 依赖指标用同一事实截面、同一分组求值（报告内截面一致性的单点保证）
        dep_results: dict[str, MetricResult] = {}
        lineage = [f"表达式 {ast.to_text()}"]
        for ref in refs:
            dep_metric = self.registry.get(ref)
            dep_query = _clone_query(query, ref_metric=dep_metric)
            dep_result = self.run(dep_query, ctx, stack)
            if dep_result.blocked_reason:
                return {}, lineage + [f"依赖 {ref} 不可计算: {dep_result.blocked_reason}"]
            dep_results[ref] = dep_result
            lineage.append(f"依赖 {ref} v{dep_metric.version} -> {dep_metric.name}")

        all_keys = set()
        for result in dep_results.values():
            all_keys.update(result.groups)

        groups: dict[tuple, GroupResult] = {}
        for key in all_keys:
            values = {}
            for ref, result in dep_results.items():
                part = result.groups.get(key)
                values[ref] = part.as_dict() if part else \
                    {"value": None, "uncertain_value": None, "uncertain": True,
                     "reason": f"分组缺少依赖 {ref}"}
            evaluated = dsl.evaluate(ast, values)

            # 比率派生：附带重算说明，明确这不是下级比率的平均
            detail = {"expression": ast.to_text(),
                      "components": {ref: values[ref] for ref in refs}}
            if metric.additivity == "ratio":
                detail["aggregation_rule"] = "按分子/分母在本分组重新计算，不平均下级比率"
            groups[key] = GroupResult(
                value=evaluated.get("value"),
                uncertain_value=evaluated.get("uncertain_value"),
                detail=detail,
                blocked_reason=evaluated.get("reason"),
            )
        return groups, lineage

    # ------------------------------------------------------------ 窗口
    def _run_cohort(self, metric: MetricDef, query: Query,
                    ctx: DataContext) -> dict[tuple, GroupResult]:
        cohort_query = _clone_query(query, ref_metric=metric)
        cohort_query.window_days = metric.window_days
        raw = BASE_MEASURES["repurchase_cohort"](ctx, cohort_query)
        groups: dict[tuple, GroupResult] = {}
        for key, result in raw.items():
            eligible = result.value or 0.0
            repeat = float(result.detail["repeat_buyers"])
            value = (repeat / eligible) if eligible else None
            uncertain_ratio = None
            if result.uncertain_value:
                # 删失家庭不能算零复购：单独给出"若全部未复购/全部复购"的提示区间
                censored = result.uncertain_value
                uncertain_ratio = {
                    "lower_bound_if_censored_all_non_repeat": value,
                    "upper_bound_if_censored_all_repeat":
                        (repeat + censored) / (eligible + censored) if eligible + censored else None,
                    "censored_first_buyers": censored,
                }
            groups[key] = GroupResult(
                value=value,
                uncertain_value=uncertain_ratio,
                detail={**result.detail, "rule": "删失家庭不计零，区间仅作敏感性提示"},
            )
        return groups


def _clone_query(query: Query, ref_metric: MetricDef) -> Query:
    return Query(
        metric_code=ref_metric.code,
        region=query.region,
        family=query.family,
        spec_id=query.spec_id,
        topic=query.topic,
        group_by=[d for d in query.group_by if d in ref_metric.eligible_dims],
        attribution_version=query.attribution_version,
    )
