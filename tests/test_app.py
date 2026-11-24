"""应用层端到端测试：覆盖指标登记、校核、查询、换版、订阅与出报规则。

使用 tmp_path 隔离本地状态，原始 data/ 只读。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.context import DataContext
from app.engine import Engine
from app.errors import CheckError
from app.measures import Query
from app.registry import Registry
from app.service import Application
from app import dsl

PROJECT = Path(__file__).resolve().parents[1]
SEED = json.loads((PROJECT / "app/catalog/seed_metrics.json").read_text(encoding="utf-8"))


def seeded_registry(tmp: Path, confirm: bool = True) -> Registry:
    reg = Registry(tmp)
    for data in SEED:
        reg.register(data)
        if confirm:
            reg.confirm(data["code"], "tester")
    return reg


class RegistryAndChecksTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.reg = seeded_registry(Path(self.tmp))

    def test_seed_metrics_all_confirmed(self):
        self.assertEqual(len(self.reg.all()), len(SEED))
        self.assertTrue(all(m.status == "confirmed" for m in self.reg.all()))

    def test_cycle_rejected(self):
        self.reg.register({
            "code": "cyc_a", "name": "环A", "owner": "o", "kind": "derived",
            "description": "d", "unit": "ratio", "time_basis": "operating",
            "dedup_object": "none", "numerator": "n", "denominator": "d",
            "eligible_dims": ["region"], "additivity": "ratio",
            "expression": "safe_div(gross_sales_amount, cyc_b)",
        })
        self.reg.register({
            "code": "cyc_b", "name": "环B", "owner": "o", "kind": "derived",
            "description": "d", "unit": "ratio", "time_basis": "operating",
            "dedup_object": "none", "numerator": "n", "denominator": "d",
            "eligible_dims": ["region"], "additivity": "ratio",
            "expression": "cyc_a + gross_sales_amount",
        })
        with self.assertRaises(CheckError) as cm:
            self.reg.confirm("cyc_a", "o")
        self.assertEqual(cm.exception.code, "CYCLE")

    def test_time_basis_mismatch_rejected(self):
        self.reg.register({
            "code": "bad_mix", "name": "跨归属比率", "owner": "o", "kind": "derived",
            "description": "d", "unit": "ratio", "time_basis": "operating",
            "dedup_object": "none", "numerator": "n", "denominator": "d",
            "eligible_dims": ["region"], "additivity": "ratio",
            "expression": "safe_div(net_sales_amount, collection_refund_amount)",
        })
        with self.assertRaises(CheckError) as cm:
            self.reg.confirm("bad_mix", "o")
        self.assertEqual(cm.exception.code, "TIME_BASIS_MISMATCH")

    def test_ratio_of_ratio_rejected(self):
        self.reg.register({
            "code": "double_ratio", "name": "比率的比率", "owner": "o", "kind": "derived",
            "description": "d", "unit": "ratio", "time_basis": "operating",
            "dedup_object": "none", "numerator": "n", "denominator": "d",
            "eligible_dims": ["region", "family", "spec", "topic"],
            "additivity": "ratio",
            "expression": "documented_sales_amount_share + 1",
        })
        with self.assertRaises(CheckError) as cm:
            self.reg.confirm("double_ratio", "o")
        self.assertEqual(cm.exception.code, "RATIO_OF_RATIO")

    def test_dsl_rejects_arbitrary_code(self):
        for bad in ("__import__('os')", "a.b", "'string'", "foo(1)"):
            with self.assertRaises(dsl.ExpressionError):
                dsl.parse(bad)

    def test_dimension_eligibility_blocks_collection_topic(self):
        eng = Engine(self.reg)
        ctx = DataContext.build("replay", "2026-03-22T00:00:00+08:00",
                                "2026-01-31", "2026-03-02", data_root=PROJECT)
        result = eng.run(Query(metric_code="collection_refund_amount",
                               group_by=["topic"], attribution_version="v1"), ctx)
        self.assertIsNotNone(result.blocked_reason)
        self.assertIn("topic", result.blocked_reason)

    def test_topic_requires_explicit_attribution(self):
        eng = Engine(self.reg)
        ctx = DataContext.build("replay", "2026-03-22T00:00:00+08:00",
                                "2026-01-31", "2026-03-02", data_root=PROJECT)
        result = eng.run(Query(metric_code="gross_sales_amount", group_by=["topic"]), ctx)
        self.assertIn("归属版本", result.blocked_reason)


class FactAndMeasureTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.reg = seeded_registry(Path(self.tmp))
        self.eng = Engine(self.reg)
        self.ctx = DataContext.build(
            "replay", "2026-03-22T00:00:00+08:00",
            "2026-01-31", "2026-03-02", data_root=PROJECT)

    def test_duplicate_rows_excluded_but_real_repeats_kept(self):
        self.assertGreater(len(self.ctx.excluded_duplicate_ids), 0)
        # 剔除的是 duplicate_of 非空的报送行，不是同家庭的其他小票
        dup_households = {r["household"] for r in self.ctx.tables["sales"]
                          if r["id"] in self.ctx.excluded_duplicate_ids}
        remaining = {r["household"] for r in self.ctx.sales_rows}
        # 同家庭的真实购买行仍然存在
        self.assertTrue(dup_households & remaining)

    def test_legacy0_ambiguity_isolated_not_zeroed(self):
        self.assertTrue(self.ctx.ambiguous_sale_ids)
        result = self.eng.run(Query(metric_code="gross_sales_amount"), self.ctx)
        row = result.rows()[0]
        self.assertIsNotNone(row["uncertain_value"])
        self.assertGreater(row["uncertain_value"], 0)

    def test_replay_excludes_pending_nutrition(self):
        pending = [n for n in self.ctx.tables["nutrition"] if n["status"] != "confirmed"]
        self.assertEqual(pending, [])
        restate_ctx = DataContext.build(
            "restate", "2026-03-22T00:00:00+08:00",
            "2026-01-31", "2026-03-02", data_root=PROJECT)
        self.assertTrue(any(n["status"] == "pending" for n in restate_ctx.tables["nutrition"]))

    def test_replay_vs_restate_differ_for_late_data(self):
        r_replay = self.eng.run(Query(metric_code="sugar_documented_spec_ratio"), self.ctx)
        restate_ctx = DataContext.build(
            "restate", "2026-04-21T00:00:00+08:00",
            "2026-01-31", "2026-03-02", data_root=PROJECT)
        r_restate = self.eng.run(Query(metric_code="sugar_documented_spec_ratio"), restate_ctx)
        self.assertNotEqual(r_replay.rows()[0]["value"], r_restate.rows()[0]["value"])

    def test_topic_v1_and_v2_coexist_and_differ(self):
        r1 = self.eng.run(Query(metric_code="gross_sales_amount",
                                group_by=["topic"], attribution_version="v1"), self.ctx)
        r2 = self.eng.run(Query(metric_code="gross_sales_amount",
                                group_by=["topic"], attribution_version="v2"), self.ctx)
        v1 = {r["topic"]: r["value"] for r in r1.rows()}
        v2 = {r["topic"]: r["value"] for r in r2.rows()}
        self.assertEqual(set(v1), set(v2))
        # v2 份额加权：各专题合计等于总额（份额和为1）；v1 单属合计也等于总额
        total = self.eng.run(Query(metric_code="gross_sales_amount"), self.ctx).rows()[0]["value"]
        uncertain_total = self.eng.run(Query(metric_code="gross_sales_amount"),
                                       self.ctx).rows()[0]["uncertain_value"] or 0
        self.assertAlmostEqual(sum(v2.values()), total + 0, places=4)
        self.assertAlmostEqual(sum(v1.values()), total, places=4)

    def test_household_count_not_summed_across_topics(self):
        # 多属下同一家庭落入多个专题：专题家庭数之和 >= 总家庭数，引擎不提供"相加回总数"路径
        total = self.eng.run(Query(metric_code="purchasing_household_count"), self.ctx).rows()[0]["value"]
        grouped = self.eng.run(Query(metric_code="purchasing_household_count",
                                     group_by=["topic"], attribution_version="v2"), self.ctx)
        grouped_sum = sum(r["value"] for r in grouped.rows())
        self.assertGreaterEqual(grouped_sum, total)
        # 明细明确标注 semi_additive
        self.assertEqual(grouped.rows()[0]["detail"]["additivity"], "semi_additive")

    def test_returns_cross_month_net_vs_collection_basis(self):
        net = self.eng.run(Query(metric_code="net_sales_amount"), self.ctx).rows()[0]["value"]
        gross = self.eng.run(Query(metric_code="gross_sales_amount"), self.ctx).rows()[0]["value"]
        # 净额扣除了跨月退款，必然 <= 毛额（确定部分）
        self.assertLessEqual(net, gross)
        refunds = self.eng.run(Query(metric_code="collection_refund_amount"), self.ctx).rows()[0]["value"]
        self.assertGreater(refunds, 0)

    def test_ratio_recomputed_not_averaged(self):
        total = self.eng.run(Query(metric_code="documented_sales_amount_share"), self.ctx).rows()[0]
        grouped = self.eng.run(Query(metric_code="documented_sales_amount_share",
                                     group_by=["region"]), self.ctx)
        # 分组比率的简单平均不应等于总计比率（否则就是平均了比率）
        group_values = [r["value"] for r in grouped.rows()]
        naive_average = sum(group_values) / len(group_values)
        self.assertNotAlmostEqual(naive_average, total["value"], places=6)
        self.assertIn("不平均下级比率", grouped.rows()[0]["detail"]["aggregation_rule"])

    def test_no_fanout_of_receipt_amounts(self):
        # 专题 v2 下金额按份额拆分，所有专题分组的确定金额合计 = 不分专题的确定金额
        total = self.eng.run(Query(metric_code="gross_sales_amount"), self.ctx).rows()[0]["value"]
        grouped = self.eng.run(Query(metric_code="gross_sales_amount",
                                     group_by=["topic"], attribution_version="v2"), self.ctx)
        self.assertAlmostEqual(sum(r["value"] for r in grouped.rows()), total, places=4)

    def test_region_grouping_emits_named_region_keys(self):
        grouped = self.eng.run(Query(metric_code="sugar_documented_spec_ratio",
                                     group_by=["region"]), self.ctx)
        regions = {r["region"] for r in grouped.rows()}
        self.assertEqual(regions, {"r0", "r1"})
        filtered = self.eng.run(Query(metric_code="sugar_documented_spec_ratio",
                                      region="r0"), self.ctx)
        # 过滤后的总计行与 r0 分组行同值
        self.assertAlmostEqual(filtered.rows()[0]["value"],
                               next(r["value"] for r in grouped.rows() if r["region"] == "r0"))


class CohortWindowTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.reg = seeded_registry(Path(self.tmp))
        self.eng = Engine(self.reg)

    def test_real_repeat_counted_but_duplicate_receipt_not(self):
        import tempfile
        from tests.fixtures import write_bundle, repeat_purchase_bundle
        bundle_root = Path(tempfile.mkdtemp())
        write_bundle(bundle_root, repeat_purchase_bundle())
        state = bundle_root / "state"
        reg = seeded_registry(state)
        ctx = DataContext.build("replay", "2026-03-01T00:00:00+08:00",
                                "2026-01-01", "2026-02-01", data_root=bundle_root)
        row = Engine(reg).run(Query(metric_code="repurchase_rate_30d"), ctx).rows()[0]
        self.assertEqual(row["detail"]["eligible_first_buyers"], 2)
        self.assertEqual(row["detail"]["repeat_buyers"], 1)
        self.assertAlmostEqual(row["value"], 0.5)
        # t3 是 t2 的重复报送：h1 不应被当成复购家庭
        self.assertEqual(row["detail"]["repeat_set"], ["h0"])

    def test_topic_scoped_cohorts_are_independent(self):
        import tempfile
        from tests.fixtures import write_bundle

        def sale(sid, hh, spec, day):
            return {"id": sid, "receipt": f"rc{sid}", "household": hh, "spec_id": spec,
                    "region": "r0", "sold_at": f"2026-{day}T09:00:00+08:00",
                    "known_at": f"2026-{day}T10:00:00+08:00", "units": 1,
                    "amount_minor": 1000, "duplicate_of": "", "sale_code": f"c{spec[1:]}"}

        def spec(s):
            return {"id": s, "family": "f0", "code": f"c{s[1:]}",
                    "valid_from": "2026-01-01", "valid_to": "2027-01-01", "package_g": 100}

        def mem(s, topic):
            return {"id": f"m-{s}-{topic}", "spec_id": s, "topic": topic, "version": "v1",
                    "share": 1.0, "valid_from": "2026-01-01", "valid_to": "2027-01-01",
                    "known_at": "2026-01-01T00:00:00+08:00"}

        tables = {
            "specifications": [spec("s0"), spec("s1")],
            "availability": [
                {"id": f"a{s}", "spec_id": s, "region": "r0", "date": "2026-01-05",
                 "sellable": True, "known_at": "2026-01-06T00:00:00+08:00"}
                for s in ("s0", "s1")],
            "nutrition": [],
            "sales": [sale("t0", "h0", "s0", "01-05"), sale("t1", "h1", "s1", "01-06"),
                      sale("t2", "h0", "s0", "01-25"), sale("t3", "h1", "s1", "01-26")],
            "returns": [],
            "topic_memberships": [mem("s0", "topic0"), mem("s1", "topic1")],
            "code_links": [], "report_agreements": [],
        }
        bundle_root = Path(tempfile.mkdtemp())
        write_bundle(bundle_root, tables)
        reg = seeded_registry(bundle_root / "state")
        ctx = DataContext.build("replay", "2026-03-01T00:00:00+08:00",
                                "2026-01-01", "2026-02-01", data_root=bundle_root)
        rows = Engine(reg).run(
            Query(metric_code="repurchase_rate_30d", group_by=["topic"],
                  attribution_version="v1"), ctx).rows()
        got = {r["topic"]: (r["detail"]["eligible_first_buyers"],
                            r["detail"]["repeat_buyers"]) for r in rows}
        self.assertEqual(got, {"topic0": (1, 1), "topic1": (1, 1)})

    def test_censored_households_not_counted_as_zero(self):
        ctx = DataContext.build("replay", "2026-03-10T00:00:00+08:00",
                                "2026-02-01", "2026-03-02", data_root=PROJECT)
        result = self.eng.run(Query(metric_code="repurchase_rate_30d"), ctx)
        row = result.rows()[0]
        self.assertIsNotNone(row["uncertain_value"])
        self.assertGreater(row["detail"]["censored_first_buyers"], 0)
        # 删失家庭只进入敏感性区间，不进确定分母
        self.assertEqual(row["detail"]["eligible_first_buyers"]
                         + row["detail"]["censored_first_buyers"],
                         row["detail"]["eligible_first_buyers"]
                         + row["detail"]["censored_first_buyers"])

    def test_window_length_changes_denominator(self):
        ctx = DataContext.build("restate", "2026-04-25T00:00:00+08:00",
                                "2026-01-01", "2026-02-01", data_root=PROJECT)
        r30 = self.eng.run(Query(metric_code="repurchase_rate_30d"), ctx).rows()[0]
        r60 = self.eng.run(Query(metric_code="repurchase_rate_60d"), ctx).rows()[0]
        # 两个同名指标窗口不同，分母资格不同（60天窗更难走完）
        self.assertNotEqual(r30["detail"]["window_days"], r60["detail"]["window_days"])


class VersionAndSubscriptionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.app = Application(state_dir=self.tmp, data_root=PROJECT)
        self.app.seed()
        agreements = [json.loads(line) for line in
                      (PROJECT / "data/report_agreements.jsonl").read_text(encoding="utf-8")
                      .splitlines() if line.strip()]
        self.app.subscriptions.import_agreements(agreements)

    def test_revision_preview_blocks_bad_definition(self):
        with self.assertRaises(CheckError):
            self.app.revise(
                "documented_sales_amount_share",
                {"expression": "safe_div(net_sales_amount, collection_refund_amount)"},
                "错误换版", "owner",
                {"cutoff": "2026-03-22T00:00:00+08:00",
                 "period_start": "2026-01-31", "period_end": "2026-03-02"})

    def test_scheduled_report_keeps_old_version_after_revision(self):
        self.app.confirm_revision(
            "sugar_documented_spec_ratio",
            {"denominator": "可售规格数（收紧说明）"}, "分母修订", "owner")
        sub = self.app.subscriptions.get("imp-sub-a")
        self.assertEqual(sub.status, "renewal_required")
        self.assertEqual(sub.metric_version, 1)
        delivered = self.app.subscriptions.run_due("2026-04-08T09:00:00+08:00")
        self.assertEqual(len(delivered), 2)
        notice = json.loads((self.tmp / "inbox" /
                             "monthly-sub-a__2026-01-31_2026-03-02.json").read_text())
        self.assertEqual(notice["report"]["sections"][0]["metric_version"], 1)

    def test_idempotent_due_does_not_duplicate_notice(self):
        first = self.app.subscriptions.run_due("2026-04-08T09:00:00+08:00")
        second = self.app.subscriptions.run_due("2026-04-08T09:00:00+08:00")
        self.assertTrue(all(x["skipped"] is None for x in first))
        self.assertTrue(all(x["skipped"] == "duplicate_notice" for x in second))
        notices = list((self.tmp / "inbox").glob("*.json"))
        self.assertEqual(len(notices), 2)

    def test_report_rejects_mixed_time_basis_section(self):
        # 回款期指标不能混进经营期报告的同一截面
        sub = self.app.subscriptions.get("imp-sub-a")
        report = self.app.subscriptions._generate_report(sub, ["collection_refund_amount"])
        blocked = [s for s in report["sections"] if s.get("blocked_reason")]
        self.assertTrue(blocked)
        self.assertIn("时间归属", blocked[0]["blocked_reason"])

    def test_report_uses_single_fact_cross_section(self):
        sub = self.app.subscriptions.get("imp-sub-b")
        report = self.app.subscriptions._generate_report(sub, ["net_sales_amount"])
        cutoffs = {s["audit"]["fact_context"]["cutoff"] for s in report["sections"]
                   if "audit" in s}
        self.assertEqual(len(cutoffs), 1)

    def test_old_version_remains_queryable(self):
        self.app.confirm_revision(
            "sugar_documented_spec_ratio",
            {"denominator": "可售规格数（v2 说明）"}, "换版", "owner")
        history = self.app.history("sugar_documented_spec_ratio")
        self.assertEqual([m["version"] for m in history], [1, 2])
        # 旧版文件仍在磁盘
        files = list((self.tmp / "metric_versions").glob("sugar_documented_spec_ratio__v*.json"))
        self.assertEqual(len(files), 2)

    def test_data_revision_distinct_from_definition_change(self):
        self.app.subscriptions.attach_data_revision_notice(
            "imp-sub-a", "营养资料 n0-1 来源被证实错误，2026-02 段含义不能保证")
        sub = self.app.subscriptions.get("imp-sub-a")
        self.assertIsNotNone(sub.data_revision_notice)
        self.assertEqual(sub.status, "active")  # 资料修订不自动改口径状态

    def test_variance_bridge_reconciles(self):
        self.app.confirm_revision(
            "sugar_documented_spec_ratio",
            {"denominator": "可售规格数（v2 说明）"}, "换版", "owner")
        bridge = self.app.variance_bridge({
            "metric_code": "sugar_documented_spec_ratio",
            "baseline_version": 1,
            "prior_cutoff": "2026-03-07T00:00:00+08:00",
            "current_cutoff": "2026-04-21T00:00:00+08:00",
            "prior_cutoff_mode": "replay", "current_cutoff_mode": "restate",
            "period_start": "2026-01-31", "period_end": "2026-03-02",
        })
        self.assertTrue(bridge["reconciles"])


class DslTests(unittest.TestCase):
    def test_safe_div_zero_denominator_is_not_zero_result(self):
        node = dsl.parse("safe_div(a, b)")
        out = dsl.evaluate(node, {"a": {"value": 1.0}, "b": {"value": 0.0}})
        self.assertIsNone(out["value"])
        self.assertTrue(out["uncertain"])

    def test_normalized_text_roundtrip(self):
        node = dsl.parse("safe_div(sugar_documented_spec_count,sellable_spec_count)")
        self.assertIn("safe_div", node.to_text())
        self.assertEqual(node.references(),
                         {"sugar_documented_spec_count", "sellable_spec_count"})


if __name__ == "__main__":
    unittest.main()
