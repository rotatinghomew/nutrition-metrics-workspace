"""指标登记册。

每条指标定义必须显式登记：分母/分子、时间归属、单位、去重对象、可展开维度、
可加性类别、归属方式（单属/多属份额）。派生指标还登记表达式与依赖。

登记册在本地状态目录里以 JSON 文件保存（append-only 版本文件 + 一个当前指针），
原始目录 `data/` 保持只读可追踪。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import RegistryError

# 系统内建的时间归属，派生公式只允许同归属指标互相运算
TIME_BASES = ("operating", "collection")
# 可展开维度（不含 period，period 是查询的窗口而非分组维度）
DIMENSIONS = ("region", "family", "spec", "topic")
# 可加性类别：
#   additive        沿任意已授权维度可加（金额、计数类）
#   semi_additive   只能按规格/门店等实体去重聚合，不能沿专题分项相加
#   ratio           比率：禁止对下级比率再平均，聚合时必须重算分子/分母
ADDITIVITY = ("additive", "semi_additive", "ratio")
# 专题归属方式：single=单属展示，multi_share=多属按获批份额
ATTRIBUTIONS = ("single", "multi_share")


@dataclass
class MetricDef:
    code: str
    name: str
    owner: str
    kind: str                          # base | derived | cohort_window
    description: str
    unit: str                          # count | minor_amount | ratio | household
    time_basis: str                    # operating | collection
    dedup_object: str                  # receipt_row | spec | household | household_first | none
    numerator: str                     # 自然语言+机器可读的分子说明
    denominator: str
    eligible_dims: list[str]
    additivity: str
    requires_attribution: str | None = None     # 若指标只能在某种专题归属下成立则登记
    expression: str | None = None                # derived：DSL 表达式
    depends_on: list[str] = field(default_factory=list)
    window_days: int | None = None               # cohort_window：复购观察窗
    cohort_basis: str | None = None              # cohort_window：首购口径说明
    version: int = 1
    status: str = "draft"                        # draft | confirmed | deprecated
    created_at: str = ""
    confirmed_at: str | None = None
    confirmed_by: str | None = None
    change_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MetricDef":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise RegistryError(f"指标 {data.get('code')} 含未知字段: {sorted(unknown)}")
        return cls(**data)


class Registry:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.version_dir = self.state_dir / "metric_versions"
        self.version_dir.mkdir(parents=True, exist_ok=True)
        self._defs: dict[str, MetricDef] = {}
        self._load()

    # ------------------------------------------------------------ 持久化
    def _load(self) -> None:
        for path in sorted(self.version_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            metric = MetricDef.from_dict(data)
            self._defs[metric.code] = metric  # 文件名带版本号，后写覆盖即最新

    def _filename(self, metric: MetricDef) -> Path:
        return self.version_dir / f"{metric.code}__v{metric.version}.json"

    def _write(self, metric: MetricDef) -> None:
        path = self._filename(metric)
        path.write_text(
            json.dumps(metric.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._defs[metric.code] = metric

    # ------------------------------------------------------------ 查询
    def get(self, code: str) -> MetricDef:
        if code not in self._defs:
            raise RegistryError(f"指标 {code} 未登记")
        return self._defs[code]

    def has(self, code: str) -> bool:
        return code in self._defs

    def all(self) -> list[MetricDef]:
        return sorted(self._defs.values(), key=lambda m: (m.kind, m.code))

    def current_versions(self) -> dict[str, int]:
        return {code: m.version for code, m in self._defs.items()}

    def history(self, code: str) -> list[MetricDef]:
        return [
            MetricDef.from_dict(json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(self.version_dir.glob(f"{code}__v*.json"))
        ]

    # ------------------------------------------------------------ 登记
    def register(self, data: dict[str, Any]) -> MetricDef:
        """登记新指标（draft）。结构合法性在此检查，语义校核在 app.checks。"""
        code = data.get("code", "").strip()
        if not code:
            raise RegistryError("指标 code 不能为空")
        if code in self._defs:
            raise RegistryError(f"指标 {code} 已存在，修订请用 revise()")
        metric = MetricDef.from_dict({**data, "version": 1, "status": "draft",
                                      "created_at": _now()})
        self._validate_shape(metric)
        self._write(metric)
        return metric

    def revise(self, code: str, changes: dict[str, Any], note: str, actor: str) -> MetricDef:
        """基于当前版本生成新草稿版本；旧版本文件保留，订阅仍可锁定旧版。"""
        old = self.get(code)
        if old.status == "draft":
            raise RegistryError(f"{code} 仍是草稿，请直接编辑草稿或先确认")
        protected = {"code", "version", "status", "created_at", "confirmed_at",
                     "confirmed_by"}
        bad = protected & set(changes)
        if bad:
            raise RegistryError(f"字段 {sorted(bad)} 不允许通过修订修改")
        new_data = old.to_dict()
        new_data.update(changes)
        new_data.update({
            "version": old.version + 1,
            "status": "draft",
            "confirmed_at": None,
            "confirmed_by": None,
            "change_note": note,
            "created_at": old.created_at,
        })
        metric = MetricDef.from_dict(new_data)
        self._validate_shape(metric)
        self._write(metric)
        return metric

    def edit_draft(self, code: str, changes: dict[str, Any]) -> MetricDef:
        metric = self.get(code)
        if metric.status != "draft":
            raise RegistryError(f"{code} 不是草稿，无法直接编辑")
        data = metric.to_dict()
        data.update(changes)
        updated = MetricDef.from_dict(data)
        self._validate_shape(updated)
        self._write(updated)
        return updated

    def confirm(self, code: str, actor: str) -> MetricDef:
        metric = self.get(code)
        # 延迟导入避免循环依赖
        from .checks import validate_definition
        validate_definition(self, metric)
        metric.status = "confirmed"
        metric.confirmed_at = _now()
        metric.confirmed_by = actor
        self._write(metric)
        return metric

    def deprecate(self, code: str, actor: str) -> MetricDef:
        metric = self.get(code)
        metric.status = "deprecated"
        metric.confirmed_by = actor
        self._write(metric)
        return metric

    # ------------------------------------------------------------ 结构检查
    @staticmethod
    def _validate_shape(metric: MetricDef) -> None:
        if metric.kind not in ("base", "derived", "cohort_window"):
            raise RegistryError(f"{metric.code}: kind 必须是 base/derived/cohort_window")
        if metric.time_basis not in TIME_BASES:
            raise RegistryError(f"{metric.code}: time_basis 必须是 {TIME_BASES}")
        if metric.additivity not in ADDITIVITY:
            raise RegistryError(f"{metric.code}: additivity 必须是 {ADDITIVITY}")
        bad_dims = set(metric.eligible_dims) - set(DIMENSIONS)
        if bad_dims:
            raise RegistryError(f"{metric.code}: 未知维度 {sorted(bad_dims)}")
        if metric.requires_attribution is not None and \
                metric.requires_attribution not in ATTRIBUTIONS:
            raise RegistryError(f"{metric.code}: requires_attribution 必须是 {ATTRIBUTIONS} 或 null")
        if not metric.numerator or not metric.denominator:
            raise RegistryError(f"{metric.code}: 分子与分母必须显式登记")
        if not metric.dedup_object:
            raise RegistryError(f"{metric.code}: 去重对象必须显式登记")
        if metric.kind == "derived":
            if not metric.expression:
                raise RegistryError(f"{metric.code}: 派生指标必须登记表达式")
        if metric.kind == "cohort_window":
            if not isinstance(metric.window_days, int) or metric.window_days <= 0:
                raise RegistryError(f"{metric.code}: 窗口指标必须登记正整数 window_days")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
