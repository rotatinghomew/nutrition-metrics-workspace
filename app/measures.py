"""基础度量：全部在安全粒度上计算。

关键防放大规则：

* 成交行（receipt 行粒度）绝不直接与按日规格资料多对多连接；
  规格级资料（营养、可售、专题）先在规格粒度上聚合为"每规格一行"的判定，再回到成交行。
* 专题多属（v2）下，金额类按获批份额 ``share`` 拆入口径；
  去重计数类（家庭数）不按份额切，也不允许沿专题分项相加。
* 歧义对象（legacy0 冲突映射、营养区间不唯一）进入 uncertain_*，缺项不当零。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from .context import DataContext, parse_day, parse_dt
from .errors import CheckError

# ---------------------------------------------------------------- 维度键
DIM_KEYS = ("region", "family", "spec", "topic")
TOTAL_KEY = "ALL"


@dataclass
class GroupResult:
    """一个分组（或总计）的度量结果。"""
    value: float | None = None
    uncertain_value: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    blocked_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "uncertain_value": self.uncertain_value,
            "detail": self.detail,
            "blocked_reason": self.blocked_reason,
        }


@dataclass
class Query:
    metric_code: str
    region: str | None = None
    family: str | None = None
    spec_id: str | None = None
    topic: str | None = None
    group_by: list[str] = field(default_factory=list)
    attribution_version: str | None = None     # v1 | v2；涉及专题时必填
    window_days: int | None = None             # 复购观察窗，由窗口指标注入


# ================================================================ 规格级判定
def _spec_master(ctx: DataContext) -> dict[str, dict]:
    return {s["id"]: s for s in ctx.specs}


def sellable_specs(ctx: DataContext, region: str | None) -> set[str]:
    """期内、在指定地区（或任一地区）有明确可售回报的规格去重集合。"""
    return sellable_specs_by_region(ctx, region).get(region or TOTAL_KEY, set())


def sellable_specs_by_region(ctx: DataContext, region: str | None) -> dict[str, set[str]]:
    """region -> 期内该地区有明确可售回报的规格集合（规格粒度，供安全分组）。"""
    start, end = ctx.period_start, ctx.period_end
    out: dict[str, set[str]] = {}
    for row in ctx.tables["availability"]:
        day = parse_day(row["date"])
        if not (start <= day < end) or not row["sellable"]:
            continue
        if region and row["region"] != region:
            continue
        out.setdefault(row["region"], set()).add(row["spec_id"])
        if not region:
            out.setdefault(TOTAL_KEY, set()).add(row["spec_id"])
    if region and region in out:
        out.setdefault(TOTAL_KEY, set()).update(out[region])
    return out


def documented_specs(ctx: DataContext) -> tuple[set[str], set[str]]:
    """有完整糖资料的规格集合，以及因区间不唯一而无法判定的规格集合。

    完整 = 在期内存在覆盖该日的营养区间，且 sugar_g 非空（replay 下已只剩 confirmed）。
    """
    start, end = ctx.period_start, ctx.period_end
    by_spec: dict[str, list[dict]] = {}
    for row in ctx.tables["nutrition"]:
        by_spec.setdefault(row["spec_id"], []).append(row)

    documented: set[str] = set()
    for spec_id, rows in by_spec.items():
        covers = [
            r for r in rows
            if parse_day(r["valid_from"]) <= start and parse_day(r["valid_to"]) >= end
            or (parse_day(r["valid_from"]) < end and parse_day(r["valid_to"]) > start)
        ]
        if any(r["sugar_g"] is not None for r in covers):
            documented.add(spec_id)
    return documented, set(ctx.ambiguous_specs)


# ================================================================ 专题归属
def membership_map(
    ctx: DataContext, version: str, day: date | None = None
) -> dict[str, list[tuple[str, float]]]:
    """规格 -> [(topic, share)]，只在规格粒度展开，绝不作用到成交行之后再连表。"""
    day = day or ctx.period_start
    out: dict[str, list[tuple[str, float]]] = {}
    for m in ctx.tables["topic_memberships"]:
        if m["version"] != version:
            continue
        if not (parse_day(m["valid_from"]) <= day < parse_day(m["valid_to"])):
            continue
        out.setdefault(m["spec_id"], []).append((m["topic"], float(m["share"])))
    return out


# ================================================================ 成交行预处理
def prepared_sales(ctx: DataContext) -> list[dict]:
    """期内有效成交行，附带规格属性与歧义标记；保持 receipt 行粒度，不做任何连接放大。"""
    return [r for r in prepared_sales_from_all(ctx)
            if ctx.period_start <= parse_dt(r["sold_at"]).date() < ctx.period_end]


def prepared_sales_from_all(ctx: DataContext) -> list[dict]:
    """cutoff 前全部非重复成交行（跨期），供首购日等需要完整历史的度量使用。"""
    master = _spec_master(ctx)
    rows = []
    for r in ctx.tables["sales"]:
        if r["id"] in ctx.excluded_duplicate_ids:
            continue
        spec = master.get(r["spec_id"])
        rows.append({
            "sale_id": r["id"],
            "receipt": r["receipt"],
            "household": r["household"],
            "spec_id": r["spec_id"],
            "family": spec["family"] if spec else None,
            "region": r["region"],
            "amount": int(r["amount_minor"]),
            "sold_at": r["sold_at"],
            "ambiguous": r["id"] in ctx.ambiguous_sale_ids,
        })
    return rows


def _filter_rows(rows: list[dict], q: Query) -> list[dict]:
    out = rows
    if q.region:
        out = [r for r in out if r["region"] == q.region]
    if q.family:
        out = [r for r in out if r["family"] == q.family]
    if q.spec_id:
        out = [r for r in out if r["spec_id"] == q.spec_id]
    if q.topic:
        # topic 过滤在带份额的分组阶段实施
        pass
    return out


# ================================================================ 度量实现
def _groups_for_row(row: dict, q: Query, membership: dict | None) -> list[tuple[tuple, float]]:
    """返回该行落入的 (维度键元组, 份额) 列表。无专题维度时每行业务只出现一次。"""
    base = {
        "region": row["region"],
        "family": row["family"],
        "spec": row["spec_id"],
        "topic": TOTAL_KEY,
    }
    if not q.group_by and not q.topic:
        return [(tuple(TOTAL_KEY for _ in DIM_KEYS), 1.0)]
    if membership is None:
        key = tuple(base[d] if d in q.group_by else TOTAL_KEY for d in DIM_KEYS)
        return [(key, 1.0)]
    topics = membership.get(row["spec_id"], [])
    if q.topic:
        topics = [t for t in topics if t[0] == q.topic]
    if not topics:
        return []
    result = []
    for topic, share in topics:
        base["topic"] = topic
        key = tuple(base[d] if d in q.group_by else TOTAL_KEY for d in DIM_KEYS)
        result.append((key, share))
    return result


def _amount_measure(ctx: DataContext, q: Query, net: bool) -> dict[tuple, GroupResult]:
    """成交金额（分）。net=True 时减去跨月退款（经营期净额，按原成交窗口归属）。"""
    rows = prepared_sales(ctx)
    rows = _filter_rows(rows, q)
    membership = membership_map(ctx, q.attribution_version) if (q.group_by and "topic" in q.group_by) or q.topic else None

    sale_ids = {r["sale_id"] for r in rows}
    returns_by_sale: dict[str, int] = {}
    if net:
        for ret in ctx.returns_for_sales(sale_ids):
            returns_by_sale[ret["sale_id"]] = returns_by_sale.get(ret["sale_id"], 0) + int(ret["amount_minor"])

    certain: dict[tuple, float] = {}
    uncertain: dict[tuple, float] = {}
    row_counts: dict[tuple, int] = {}
    for row in rows:
        amount = row["amount"] - returns_by_sale.get(row["sale_id"], 0) if net else row["amount"]
        for key, share in _groups_for_row(row, q, membership):
            bucket = uncertain if row["ambiguous"] else certain
            bucket[key] = bucket.get(key, 0.0) + amount * share
            row_counts[key] = row_counts.get(key, 0) + 1
    return {
        key: GroupResult(
            value=certain.get(key, 0.0),
            uncertain_value=uncertain.get(key),
            detail={"receipt_rows": row_counts.get(key, 0), "unit": "minor_amount",
                    "net_of_returns": net},
        )
        for key in set(certain) | set(uncertain)
    }


def _documented_amount_measure(ctx: DataContext, q: Query) -> dict[tuple, GroupResult]:
    """有完整糖资料规格的成交金额（份额加权，仅在专题口径下使用份额）。"""
    documented, ambiguous_specs = documented_specs(ctx)
    rows = [r for r in _filter_rows(prepared_sales(ctx), q) if r["spec_id"] in documented]
    membership = membership_map(ctx, q.attribution_version) if (q.group_by and "topic" in q.group_by) or q.topic else None
    certain: dict[tuple, float] = {}
    uncertain: dict[tuple, float] = {}
    for row in rows:
        for key, share in _groups_for_row(row, q, membership):
            bucket = uncertain if (row["ambiguous"] or row["spec_id"] in ambiguous_specs) else certain
            bucket[key] = bucket.get(key, 0.0) + row["amount"] * share
    return {
        key: GroupResult(value=certain.get(key, 0.0), uncertain_value=uncertain.get(key),
                         detail={"unit": "minor_amount"})
        for key in set(certain) | set(uncertain)
    }


def _household_measure(ctx: DataContext, q: Query, documented_only: bool = False) -> dict[tuple, GroupResult]:
    """实际购买家庭去重数（semi_additive：沿专题分项不可加）。

    documented_only=True 时只保留购买过"有完整糖资料规格"的家庭，
    作为家庭覆盖率的分子（与分母同一去重对象 household）。
    """
    rows = _filter_rows(prepared_sales(ctx), q)
    if documented_only:
        documented, ambiguous_specs = documented_specs(ctx)
        rows = [r for r in rows if r["spec_id"] in documented]
    else:
        ambiguous_specs = set()
    membership = membership_map(ctx, q.attribution_version) if (q.group_by and "topic" in q.group_by) or q.topic else None
    certain: dict[tuple, set[str]] = {}
    uncertain: dict[tuple, set[str]] = {}
    for row in rows:
        for key, _share in _groups_for_row(row, q, membership):
            bucket = uncertain if (row["ambiguous"] or row["spec_id"] in ambiguous_specs) else certain
            bucket.setdefault(key, set()).add(row["household"])
    out = {}
    for key in set(certain) | set(uncertain):
        out[key] = GroupResult(
            value=float(len(certain.get(key, set()))),
            uncertain_value=float(len(uncertain.get(key, set()))) if uncertain.get(key) else None,
            detail={"dedup": "household", "additivity": "semi_additive"},
        )
    return out


def _sellable_count_measure(ctx: DataContext, q: Query) -> dict[tuple, GroupResult]:
    """可售规格数（规格粒度去重）。region 过滤/分组作用在可售回报上。"""
    by_region = sellable_specs_by_region(ctx, q.region)
    master = _spec_master(ctx)
    membership = membership_map(ctx, q.attribution_version) if "topic" in q.group_by or q.topic else None
    # 不按地区分组时只输出 ALL 行（即使底层按地区收集）
    region_keys = [r for r in by_region if r != TOTAL_KEY] if "region" in q.group_by else [TOTAL_KEY]
    certain: dict[tuple, set[str]] = {}
    for region_key in region_keys:
        specs = by_region.get(region_key, set()) if region_key != TOTAL_KEY else by_region.get(TOTAL_KEY, set())
        for spec_id in specs:
            family = master[spec_id]["family"]
            if q.family and family != q.family:
                continue
            if q.spec_id and spec_id != q.spec_id:
                continue
            base = {"region": region_key, "family": family, "spec": spec_id, "topic": TOTAL_KEY}
            if membership is not None:
                topics = membership.get(spec_id, [])
                if q.topic:
                    topics = [t for t in topics if t[0] == q.topic]
                if not topics:
                    continue
                for topic, _share in topics:
                    base["topic"] = topic
                    key = tuple(base[d] if d in q.group_by else TOTAL_KEY for d in DIM_KEYS)
                    certain.setdefault(key, set()).add(spec_id)
            else:
                key = tuple(base[d] if d in q.group_by else TOTAL_KEY for d in DIM_KEYS)
                certain.setdefault(key, set()).add(spec_id)
    return {
        key: GroupResult(value=float(len(ids)), detail={"dedup": "spec", "unit": "count"})
        for key, ids in certain.items()
    }


def _documented_count_measure(ctx: DataContext, q: Query) -> dict[tuple, GroupResult]:
    """有完整糖资料的可售规格数；区间不唯一的规格进 uncertain。"""
    by_region = sellable_specs_by_region(ctx, q.region)
    documented, ambiguous_specs = documented_specs(ctx)
    master = _spec_master(ctx)
    membership = membership_map(ctx, q.attribution_version) if "topic" in q.group_by or q.topic else None
    region_keys = [r for r in by_region if r != TOTAL_KEY] if "region" in q.group_by else [TOTAL_KEY]
    certain: dict[tuple, set[str]] = {}
    uncertain: dict[tuple, set[str]] = {}
    for region_key in region_keys:
        specs = by_region.get(region_key, set()) if region_key != TOTAL_KEY else by_region.get(TOTAL_KEY, set())
        for spec_id in specs:
            family = master[spec_id]["family"]
            if q.family and family != q.family:
                continue
            if q.spec_id and spec_id != q.spec_id:
                continue
            base = {"region": region_key, "family": family, "spec": spec_id, "topic": TOTAL_KEY}
            topics = membership.get(spec_id, []) if membership is not None else [(TOTAL_KEY, 1.0)]
            if q.topic:
                topics = [t for t in topics if t[0] == q.topic]
            for topic, _share in topics:
                base["topic"] = topic
                key = tuple(base[d] if d in q.group_by else TOTAL_KEY for d in DIM_KEYS)
                if spec_id in ambiguous_specs:
                    uncertain.setdefault(key, set()).add(spec_id)
                elif spec_id in documented:
                    certain.setdefault(key, set()).add(spec_id)
    out = {}
    for key in set(certain) | set(uncertain):
        out[key] = GroupResult(
            value=float(len(certain.get(key, set()))),
            uncertain_value=float(len(uncertain.get(key, set()))) if uncertain.get(key) else None,
            detail={"dedup": "spec", "unit": "count"},
        )
    return out


def _repurchase_measure(ctx: DataContext, q: Query) -> dict[tuple, GroupResult]:
    """复购窗口度量：返回 (窗口内已完成首购家庭数, 其中复购家庭数)。

    复购只认同一指标定义下、已走完观察窗的首购家庭；
    重复票据已在 context 层剔除，真实的第二次购买保留。
    专题口径下首购日按"家庭×专题"分别计算：家庭在某专题的首购，不带到其他专题。
    """
    window = q.window_days
    if not window:
        raise CheckError("WINDOW", "复购队列度量缺少 window_days")
    membership = membership_map(ctx, q.attribution_version) if (
        q.group_by and "topic" in q.group_by) or q.topic else None

    # 全部历史（cutoff 前、非重复）按查询维度过滤，作为首购/复购的同一作用域
    history_rows = _filter_rows(prepared_sales_from_all(ctx), q)

    # 每个分组键 -> 家庭购买日集合；专题下同一家庭在不同专题有独立的时间线
    days_by_group: dict[tuple, dict[str, set[date]]] = {}
    for row in history_rows:
        for key, _share in _groups_for_row(row, q, membership):
            days_by_group.setdefault(key, {}).setdefault(row["household"], set()).add(
                parse_dt(row["sold_at"]).date())

    start, end = ctx.period_start, ctx.period_end
    cutoff_day = ctx.cutoff.date()
    eligible: dict[tuple, set[str]] = {}
    repeaters: dict[tuple, set[str]] = {}
    censored: dict[tuple, set[str]] = {}

    for key, household_days in days_by_group.items():
        for hh, days in household_days.items():
            first_day = min(days)
            if not (start <= first_day < end):
                continue  # 首购不在本期，不进入本期队列
            window_end = first_day.fromordinal(first_day.toordinal() + window)
            if window_end > cutoff_day:
                censored.setdefault(key, set()).add(hh)
                continue
            eligible.setdefault(key, set()).add(hh)
            if any(first_day < d < window_end for d in days):
                repeaters.setdefault(key, set()).add(hh)

    out = {}
    for key in set(eligible) | set(censored):
        elig = eligible.get(key, set())
        out[key] = GroupResult(
            value=float(len(elig)),
            uncertain_value=float(len(censored.get(key, set()))) if censored.get(key) else None,
            detail={
                "dedup": "household_first",
                "window_days": window,
                "eligible_first_buyers": len(elig),
                "repeat_buyers": len(repeaters.get(key, set())),
                "censored_first_buyers": len(censored.get(key, set())),
                "repeat_set": sorted(repeaters.get(key, set())),
            },
        )
    # 复购家庭数作为伴生度量，由引擎通过 detail 组装比率
    return out


# 度量注册表：code -> 计算函数。派生指标不在此列，由 dsl 求值。
BASE_MEASURES: dict[str, Callable[[DataContext, Query], dict[tuple, GroupResult]]] = {
    "sellable_spec_count": _sellable_count_measure,
    "sugar_documented_spec_count": _documented_count_measure,
    "gross_sales_amount": lambda ctx, q: _amount_measure(ctx, q, net=False),
    "net_sales_amount": lambda ctx, q: _amount_measure(ctx, q, net=True),
    "documented_sales_amount": _documented_amount_measure,
    "purchasing_household_count": lambda ctx, q: _household_measure(ctx, q),
    "documented_household_count": lambda ctx, q: _household_measure(ctx, q, documented_only=True),
    "repurchase_cohort": _repurchase_measure,
}

# 回款期度量：按实际发生日看退款金额（不充当经营期分母，只可对照）
COLLECTION_MEASURES: dict[str, Callable[[DataContext, Query], dict[tuple, GroupResult]]] = {
    "collection_refund_amount": None,  # 占位，下方定义后注册
}


def _collection_refund_measure(ctx: DataContext, q: Query) -> dict[tuple, GroupResult]:
    """仓储回款金额：按退款实际发生日归属本期，金额单位分。"""
    master = _spec_master(ctx)
    sale_by_id = {r["id"]: r for r in ctx.tables["sales"]}
    certain: dict[tuple, float] = {}
    row_count: dict[tuple, int] = {}
    for ret in ctx.period_returns:
        sale = sale_by_id.get(ret["sale_id"])
        if sale is None or sale["id"] in ctx.excluded_duplicate_ids:
            continue
        spec = master.get(sale["spec_id"], {})
        row = {"region": sale["region"], "family": spec.get("family"),
               "spec_id": sale["spec_id"]}
        if q.region and row["region"] != q.region:
            continue
        if q.family and row["family"] != q.family:
            continue
        if q.spec_id and row["spec_id"] != q.spec_id:
            continue
        key = tuple(row.get(d, TOTAL_KEY) if d in q.group_by else TOTAL_KEY for d in DIM_KEYS)
        certain[key] = certain.get(key, 0.0) + int(ret["amount_minor"])
        row_count[key] = row_count.get(key, 0) + 1
    return {
        key: GroupResult(value=v, detail={"receipt_rows": row_count.get(key, 0),
                                         "unit": "minor_amount", "time_basis": "collection"})
        for key, v in certain.items()
    }


COLLECTION_MEASURES["collection_refund_amount"] = _collection_refund_measure
BASE_MEASURES.update(COLLECTION_MEASURES)
