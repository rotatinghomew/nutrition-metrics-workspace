"""指标定义校核。

校核发生在两个时刻：

1. 指标确认前（``validate_definition``）：结构、表达式、循环、时间归属、维度资格；
2. 业务查询时（``validate_query``）：这次请求的维度/归属/截止规则是否被该指标授权。

任何不成立的组合都抛出带具体原因的 :class:`CheckError`，
业务端据此展示"为什么不可计算"，而不是得到空表。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import dsl
from .errors import CheckError
from .registry import DIMENSIONS, Registry, MetricDef
from .measures import Query


# ---------------------------------------------------------------- 定义校核
def validate_definition(registry: Registry, metric: MetricDef) -> None:
    if metric.kind == "derived":
        _validate_derived(registry, metric)
    elif metric.kind == "cohort_window":
        _validate_cohort(registry, metric)
    else:
        _validate_base(registry, metric)


def _validate_base(registry: Registry, metric: MetricDef) -> None:
    from .measures import BASE_MEASURES
    if metric.code not in BASE_MEASURES:
        raise CheckError("UNKNOWN_BASE", f"基础度量 {metric.code} 没有计算实现，无法确认")
    if metric.additivity == "ratio":
        raise CheckError("RATIO_BASE", f"基础度量 {metric.code} 不能登记为比率，比率必须是派生定义")


def _validate_derived(registry: Registry, metric: MetricDef) -> None:
    try:
        ast = dsl.parse(metric.expression or "")
    except dsl.ExpressionError as exc:
        raise CheckError("EXPRESSION", f"{metric.code} 表达式无法解析: {exc}") from exc

    refs = ast.references()
    if not refs:
        raise CheckError("NO_REFERENCE", f"{metric.code} 是派生指标但表达式未引用任何指标")
    # 先做循环检测，使草稿之间的循环也能被准确报出
    _detect_cycle(registry, metric.code, refs)
    for ref in sorted(refs):
        if not registry.has(ref):
            raise CheckError("MISSING_DEP", f"{metric.code} 依赖的 {ref} 尚未登记")
        dep = registry.get(ref)
        if dep.status == "draft":
            raise CheckError("DRAFT_DEP", f"{metric.code} 依赖草稿指标 {ref}，请先确认依赖")
        # 时间归属必须一致：经营期与回款期名字再像也不能相除
        if dep.time_basis != metric.time_basis:
            raise CheckError(
                "TIME_BASIS_MISMATCH",
                f"{metric.code}({metric.time_basis}) 与依赖 {ref}({dep.time_basis}) "
                "时间归属不同，不能组合为一个比率",
            )
        # 维度资格取交集：派生指标只能展开所有依赖都支持的维度
        unsupported = set(metric.eligible_dims) - set(dep.eligible_dims)
        if unsupported:
            raise CheckError(
                "DIM_NOT_ELIGIBLE",
                f"{metric.code} 请求了依赖 {ref} 不支持的维度 {sorted(unsupported)}",
            )
        # 专题归属要求必须一致，禁止一份查询暗中从单属切到多属
        if metric.requires_attribution and dep.requires_attribution \
                and metric.requires_attribution != dep.requires_attribution:
            raise CheckError(
                "ATTRIBUTION_CONFLICT",
                f"{metric.code} 要求 {metric.requires_attribution}，依赖 {ref} 要求 "
                f"{dep.requires_attribution}，归属方式冲突",
            )

    # 比率类派生：分子分母的去重对象必须兼容，且禁止"门店平均值继续平均"式登记
    if metric.additivity == "ratio":
        for ref in refs:
            dep = registry.get(ref)
            if dep.additivity == "ratio":
                raise CheckError(
                    "RATIO_OF_RATIO",
                    f"{metric.code} 直接引用比率指标 {ref}；比率不能对比率再平均，"
                    "请改为引用其分子/分母基础度量后重算",
                )
    _detect_cycle(registry, metric.code, refs)


def _validate_cohort(registry: Registry, metric: MetricDef) -> None:
    if not metric.window_days or metric.window_days <= 0:
        raise CheckError("WINDOW", f"{metric.code} 观察窗必须为正整数天数")
    if metric.time_basis != "operating":
        raise CheckError("COHORT_BASIS", f"复购窗口 {metric.code} 只能按经营期（首购窗口）归属")
    # 专题展开在查询时仍须显式选择 v1/v2；定义层不强制固定归属，两种定义可并存


def _detect_cycle(registry: Registry, start: str, refs: set[str]) -> None:
    visiting: set[str] = set()

    def walk(code: str, chain: list[str]) -> None:
        if code in visiting:
            raise CheckError("CYCLE", f"检测到循环依赖: {' -> '.join(chain + [code])}")
        visiting.add(code)
        if registry.has(code):
            dep = registry.get(code)
            if dep.kind == "derived" and dep.expression:
                for ref in dsl.parse(dep.expression).references():
                    walk(ref, chain + [code])
        visiting.discard(code)

    # 起点自身也要入图，才能发现 A->B->A 这类经起点闭合的环
    visiting.add(start)
    try:
        for ref in refs:
            walk(ref, [start])
    finally:
        visiting.discard(start)


# ---------------------------------------------------------------- 查询校核
@dataclass
class QueryDecision:
    ok: bool
    reason: str | None
    attribution_version: str | None


def validate_query(registry: Registry, metric: MetricDef, query: Query) -> QueryDecision:
    # 草稿指标不接受业务查询
    if metric.status == "draft":
        return QueryDecision(False, f"指标 {metric.code} 仍是草稿，未经负责人确认，不能对外查询", None)

    # 维度资格
    for dim in query.group_by:
        if dim not in DIMENSIONS:
            return QueryDecision(False, f"未知维度 {dim}", None)
        if dim not in metric.eligible_dims:
            return QueryDecision(False,
                                 _dim_reason(metric, dim), None)
    for dim, value in (("region", query.region), ("family", query.family),
                       ("spec", query.spec_id), ("topic", query.topic)):
        if value is not None and dim not in metric.eligible_dims:
            return QueryDecision(False, _dim_reason(metric, dim), None)

    # 专题归属：涉及专题必须显式指定版本，且指标登记的归属方式必须与版本语义匹配
    uses_topic = bool(query.group_by and "topic" in query.group_by) or bool(query.topic)
    if uses_topic:
        if not query.attribution_version:
            return QueryDecision(False, "涉及专题时必须显式选择归属版本 v1（单属）或 v2（多属份额）", None)
        required = metric.requires_attribution
        if required == "single" and query.attribution_version != "v1":
            return QueryDecision(False,
                                 f"指标 {metric.code} 登记为单属口径，只能使用 v1；"
                                 "v2 是多属份额，混用会改变分母含义", None)
        if required == "multi_share" and query.attribution_version != "v2":
            return QueryDecision(False,
                                 f"指标 {metric.code} 登记为获批份额口径，只能使用 v2", None)
    elif query.attribution_version:
        # 不按专题展开时，归属版本不影响结果，但记录下来保持截面一致
        pass

    # 比率指标的聚合规则在引擎内强制（分组时重算分子/分母，绝不平均下级比率）。
    # 专题口径只要显式选择 v1/v2 即满足"不暗中切换"的要求，两种定义可并存。
    return QueryDecision(True, None, query.attribution_version)


def _dim_reason(metric: MetricDef, dim: str) -> str:
    explain = {
        "region": "该指标的分母是全地区规格/家庭集合，登记时未授权地区展开",
        "family": "该指标未登记商品家族（family）维度资格",
        "spec": "该指标是去重计数/比率，规格级展开会破坏登记的去重对象 "
                f"{metric.dedup_object}",
        "topic": "该指标未登记专题归属方式，展开专题必须先固定单属(v1)或多属份额(v2)",
    }
    return (
        f"指标 {metric.code}（{metric.name}）没有资格使用维度 {dim}：{explain.get(dim, '未授权')}。"
        f"已登记可展开维度：{metric.eligible_dims}；去重对象：{metric.dedup_object}"
    )
