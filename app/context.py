"""事实截面与查询上下文。

所有口径计算都只面对 :class:`DataContext` 暴露的安全粒度数据，
不允许把成交行直接与按日规格资料多对多连接（防止小票金额被放大）。

两种事实截止规则：

* ``replay``（复原）：只使用 cutoff 时点之前已经获知的资料，按当时已知复原旧报告。
* ``restate``（重述）：使用 cutoff 时点之前事实发生的全部已确认资料，
  用后来确认的事实重述同一经营期（仍以 cutoff 排除更晚的修订，保证可重复）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from materials import read_bundle


def parse_dt(value: str) -> datetime:
    """解析带时区的 ISO 8601 时点。"""
    return datetime.fromisoformat(value)


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def _known_ok(row: dict[str, Any], cutoff: datetime) -> bool:
    return parse_dt(row["known_at"]) <= cutoff


@dataclass
class DataContext:
    """一次指标计算使用的完整事实截面。

    事实表全部预聚合到安全粒度，明细行通过 ``*_rows`` 暴露用于审计与不确定标注。
    """

    data_root: Path
    mode: str                      # replay | restate
    cutoff: datetime
    period_start: date
    period_end: date               # 不含该日
    tables: dict[str, list[dict]]
    # 歧义/缺项隔离：这些对象不能当作 0，只能进入不确定区
    ambiguous_sale_ids: set[str] = field(default_factory=set)
    ambiguous_specs: set[str] = field(default_factory=set)
    excluded_duplicate_ids: set[str] = field(default_factory=set)
    cutoff_notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ 构造
    @classmethod
    def build(
        cls,
        mode: str,
        cutoff: datetime | str,
        period_start: date | str,
        period_end: date | str,
        data_root: str | Path | None = None,
    ) -> "DataContext":
        if mode not in ("replay", "restate"):
            raise ValueError(f"未知事实截止规则: {mode}")
        if isinstance(cutoff, str):
            cutoff = parse_dt(cutoff)
        if isinstance(period_start, str):
            period_start = parse_day(period_start)
        if isinstance(period_end, str):
            period_end = parse_day(period_end)
        if period_end <= period_start:
            raise ValueError("经营期结束日必须晚于开始日（结束边界不含当日）")

        _schema, tables = read_bundle(data_root)
        ctx = cls(
            data_root=Path(data_root) if data_root else Path.cwd(),
            mode=mode,
            cutoff=cutoff,
            period_start=period_start,
            period_end=period_end,
            tables=tables,
        )
        ctx._apply_cutoff()
        ctx._detect_ambiguity()
        return ctx

    # ------------------------------------------------------------------ 截止
    def _apply_cutoff(self) -> None:
        """按截止规则过滤事实。

        replay 与 restate 都遵守 ``known_at <= cutoff``（否则重述不可重复）；
        区别在于 replay 只接受已确认来源（nutrition.confirmed、code_links.confirmed），
        restate 接受截止前到达的 pending 资料——那是"后来确认的事实"。
        """
        notes = self.cutoff_notes
        cutoff = self.cutoff

        nutrition = []
        for row in self.tables["nutrition"]:
            if not _known_ok(row, cutoff):
                notes.append(f"营养资料 {row['id']} 在 cutoff 之后到达，未纳入")
                continue
            if self.mode == "replay" and row["status"] != "confirmed":
                notes.append(f"replay 排除未确认营养资料 {row['id']}（status={row['status']}）")
                continue
            nutrition.append(row)

        memberships = [r for r in self.tables["topic_memberships"] if _known_ok(r, cutoff)]
        availability = [r for r in self.tables["availability"] if _known_ok(r, cutoff)]
        sales = [r for r in self.tables["sales"] if _known_ok(r, cutoff)]
        returns = [r for r in self.tables["returns"] if _known_ok(r, cutoff)]
        # 换码链接的歧义判定需要看到截止前全部来件（含未确认），
        # 因此保留一份完整链接副本；replay 的计算口径仍只使用 confirmed 链接。
        all_links = [r for r in self.tables["code_links"] if _known_ok(r, cutoff)]
        links = all_links if self.mode == "restate" else [r for r in all_links if r["confirmed"]]

        self.tables = {
            **self.tables,
            "nutrition": nutrition,
            "code_links": links,
            "code_links_all_known": all_links,
            "topic_memberships": memberships,
            "availability": availability,
            "sales": sales,
            "returns": returns,
        }

        # 重复报送行：登记并剔除。注意这只剔除"重复票据"，不影响同一家庭的真实二次购买。
        self.excluded_duplicate_ids = {
            r["id"] for r in sales if r["duplicate_of"]
        }

    def _detect_ambiguity(self) -> None:
        """标出无法唯一解释的对象。

        1. sale_code=legacy0 的成交行：code_links 中存在两个互相冲突且都未确认的候选规格，
           暂用 spec_id 不能消除歧义（资料协议明确要求）。
        2. 营养资料有效区间在同一规格上不唯一（重叠/缺口边界）时，相关规格标记为不确定。
        """
        # 歧义判定对 replay/restate 一致：只看截止前已到达的来件，
        # 未确认=不能用来消歧；已确认且唯一才允许解析。
        links = self.tables["code_links_all_known"]
        by_old: dict[str, list[dict]] = {}
        for lk in links:
            by_old.setdefault(lk["old_code"], []).append(lk)
        conflicting: set[str] = set()
        for code, candidates in by_old.items():
            confirmed_specs = {lk["new_spec"] for lk in candidates if lk["confirmed"]}
            if len(confirmed_specs) == 1:
                continue  # 唯一确认映射，可解析
            # 零确认或多个冲突确认候选 -> 歧义，暂用 spec_id 不能替代映射判定
            conflicting.add(code)
        self.ambiguous_sale_ids = {
            r["id"]
            for r in self.tables["sales"]
            if r["sale_code"] in conflicting
        }

        by_spec: dict[str, list[dict]] = {}
        for row in self.tables["nutrition"]:
            by_spec.setdefault(row["spec_id"], []).append(row)
        for spec_id, rows in by_spec.items():
            intervals = sorted((parse_day(r["valid_from"]), parse_day(r["valid_to"])) for r in rows)
            for (a_start, a_end), (b_start, b_end) in zip(intervals, intervals[1:]):
                if a_end > b_start:  # 区间重叠 -> 同一点有两个适用版本
                    self.ambiguous_specs.add(spec_id)
                    break

    # ------------------------------------------------------------------ 访问
    @property
    def sales_rows(self) -> list[dict]:
        """期内、非重复报送的成交行（保留 legacy0 歧义行，由度量决定是否隔离）。"""
        rows = []
        for r in self.tables["sales"]:
            if r["id"] in self.excluded_duplicate_ids:
                continue
            sold = parse_dt(r["sold_at"]).date()
            if self.period_start <= sold < self.period_end:
                rows.append(r)
        return rows

    @property
    def period_returns(self) -> list[dict]:
        """期内实际发生的退款（回款期口径使用发生日，不按成交日追溯）。"""
        out = []
        for r in self.tables["returns"]:
            occurred = parse_dt(r["occurred_at"]).date()
            if self.period_start <= occurred < self.period_end:
                out.append(r)
        return out

    def returns_for_sales(self, sale_ids: set[str]) -> list[dict]:
        """给定成交行集合的全部退款，不限退款发生月份（经营期净额需要跨月退款）。"""
        return [r for r in self.tables["returns"] if r["sale_id"] in sale_ids]

    @property
    def specs(self) -> list[dict]:
        return self.tables["specifications"]

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "cutoff": self.cutoff.isoformat(),
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "ambiguous_sale_count": len(self.ambiguous_sale_ids),
            "ambiguous_spec_count": len(self.ambiguous_specs),
            "excluded_duplicate_count": len(self.excluded_duplicate_ids),
            "cutoff_notes": self.cutoff_notes,
        }
