"""构造隔离的迷你资料束，用于复购窗口等需要特定行为数据的测试。"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "data/schema.json")
                    .read_text(encoding="utf-8"))


def write_bundle(root: Path, tables: dict[str, list[dict]]) -> Path:
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in SCHEMA["tables"]:
        rows = tables.get(name, [])
        (data_dir / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    (data_dir / "schema.json").write_text(json.dumps(SCHEMA, ensure_ascii=False),
                                          encoding="utf-8")
    return root


def repeat_purchase_bundle() -> dict[str, list[dict]]:
    """h0 有真实二次购买；h1 的第二行是重复报送，不得算复购。"""
    spec = {"id": "s0", "family": "f0", "code": "c0",
            "valid_from": "2026-01-01", "valid_to": "2027-01-01", "package_g": 100}
    sale = lambda sid, receipt, hh, day, dup: {  # noqa: E731
        "id": sid, "receipt": receipt, "household": hh, "spec_id": "s0", "region": "r0",
        "sold_at": f"2026-{day}T09:00:00+08:00",
        "known_at": f"2026-{day}T10:00:00+08:00", "units": 1, "amount_minor": 1000,
        "duplicate_of": dup, "sale_code": "c0",
    }
    return {
        "specifications": [spec],
        "availability": [{"id": "a0", "spec_id": "s0", "region": "r0",
                          "date": "2026-01-05", "sellable": True,
                          "known_at": "2026-01-06T00:00:00+08:00"}],
        "nutrition": [{"id": "n0", "spec_id": "s0", "valid_from": "2026-01-01",
                       "valid_to": "2027-01-01", "known_at": "2026-01-06T00:00:00+08:00",
                       "basis": "per100g", "basis_g": 100, "sugar_g": 5.0,
                       "status": "confirmed"}],
        "sales": [
            sale("t0", "rc0", "h0", "01-05", ""),
            sale("t1", "rc1", "h0", "01-20", ""),       # 30天窗内真实复购
            sale("t2", "rc2", "h1", "01-10", ""),
            sale("t3", "rc3", "h1", "01-11", "t2"),     # 重复报送，不是第二次购买
        ],
    }
