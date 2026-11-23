"""工作台引擎的单元测试：虚构小场景逐项验证机制。"""

import io
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trustee_workbench import (  # noqa: E402
    Allocation,
    Caliber,
    Claim,
    LedgerEntry,
    Part,
    PartStatus,
    Rule,
    RuleKind,
    Scope,
    Term,
    Workbench,
    WorkbenchError,
)
from trustee_workbench.scenario import run_demo  # noqa: E402

D = date(2026, 1, 1)


def make_wb() -> Workbench:
    wb = Workbench("（测试）核验计划", total=100000)
    wb.add_term(Term("A1", 1, D, "说明书", "总资金十万元。不得用于常规运维。"))
    wb.add_term(Term("A2", 2, D, "附件", "甲试点限3万。研发保留款1万限甲研发。试点自筹三成。",
                     refs=("A1",)))
    wb.add_rule(Rule("cap-jia", RuleKind.CAP, Scope.PROJECT, "甲试点",
                      ("A2",), "甲试点限3万", amount=30000))
    wb.add_rule(Rule("res-rd", RuleKind.RESERVE, Scope.GLOBAL, "研发保留款",
                      ("A2",), "研发保留款1万限列名研发项目",
                      amount=10000, projects=("甲研发",)))
    wb.add_rule(Rule("ratio", RuleKind.SELF_RATIO, Scope.PROJECT, "试点共担",
                      ("A2",), "自筹至少三成", ratio=(3, 10), projects=("甲试点",)))
    wb.add_rule(Rule("exc-om", RuleKind.EXCLUSION, Scope.GLOBAL, "常规运维",
                      ("A1",), "不得用于常规运维", purposes=("常规运维",)))
    wb.add_caliber(Caliber("示范方向", ("甲试点",), ("技术验证",), ("A2",)))
    return wb


def add_claim(wb: Workbench, claim_id: str, parts: list[Part],
              splittable: bool = True, total: int | None = None) -> None:
    wb.submit(Claim(claim_id, D, splittable, f"INV-{claim_id}", "测试申请",
                    total if total is not None else sum(p.amount for p in parts)),
              parts)


class ReviewMechanicsTest(unittest.TestCase):
    def test_ratio_caps_payable(self):
        wb = make_wb()
        add_claim(wb, "Q1", [Part("Q1-1", "Q1", "仪器验证", 20000,
                                  candidates=("甲试点",), self_funds=8000,
                                  object_key="甲试点", purpose="技术验证")])
        (p,) = wb.review("Q1")
        self.assertEqual(p.status, PartStatus.PAYABLE)
        self.assertEqual(p.payable, 18666)  # 8000×7/3，超出部分不予支付

    def test_exclusion_rejects_with_basis(self):
        wb = make_wb()
        add_claim(wb, "Q2", [Part("Q2-1", "Q2", "年度运维", 5000,
                                  object_key="甲试点", purpose="常规运维")])
        (p,) = wb.review("Q2")
        self.assertEqual(p.status, PartStatus.REJECTED)
        self.assertTrue(any("A1" in b for b in p.basis))

    def test_blocked_by_composition_then_supplemented(self):
        wb = make_wb()
        add_claim(wb, "Q3", [Part("Q3-1", "Q3", "测试费", 5000,
                                  candidates=("甲试点",), self_funds=3000,
                                  object_key="甲试点", composition_known=False)])
        (p,) = wb.review("Q3")
        self.assertEqual(p.status, PartStatus.BLOCKED)
        self.assertIn("费用构成", p.missing)
        wb.supplement("Q3-1", "PF1", purpose="技术验证", composition_known=True,
                      note="构成已载明")
        self.assertEqual(p.status, PartStatus.PAYABLE)

    def test_blocked_by_timing_and_identity(self):
        wb = make_wb()
        add_claim(wb, "Q4", [Part("Q4-1", "Q4", "某中心服务费", 5000,
                                  timing_known=False)])
        (p,) = wb.review("Q4")
        self.assertEqual(p.status, PartStatus.BLOCKED)
        self.assertIn("实际对象身份", p.missing)
        wb.supplement("Q4-1", "PF2", object_key="甲试点", purpose="技术验证",
                      timing_known=True, self_funds=3000, note="合同明确对象与时点")
        self.assertEqual(p.status, PartStatus.PAYABLE)

    def test_non_splittable_pends_whole_claim(self):
        wb = make_wb()
        add_claim(wb, "Q5", [
            Part("Q5-1", "Q5", "验证费", 5000, candidates=("甲试点",),
                 self_funds=3000, object_key="甲试点", purpose="技术验证"),
            Part("Q5-2", "Q5", "不明费", 1000, object_key="甲试点",
                 composition_known=False),
        ], splittable=False)
        parts = wb.review("Q5")
        self.assertEqual(parts[0].status, PartStatus.BLOCKED)  # 独立成立部分随同暂缓
        with self.assertRaises(WorkbenchError):
            wb.approve("Q5", {"Q5-1": "甲试点"})


class AttributionTest(unittest.TestCase):
    def test_lockout_reason_when_crowding_commitment(self):
        wb = make_wb()
        wb.ingest_ledger(LedgerEntry("E0", "commitment", D, "已签承诺", None, [
            Allocation("通用额度", "甲试点", "技术验证", 25000, "甲试点")]))
        add_claim(wb, "Q6", [Part("Q6-1", "Q6", "验证费", 8000,
                                  candidates=("甲试点",), self_funds=5000,
                                  object_key="甲试点", purpose="技术验证")])
        (p,) = wb.review("Q6")
        self.assertEqual(p.status, PartStatus.NO_ROUTE)
        self.assertIn("锁死已签未付", p.options[0].reasons[0])

    def test_single_attribution_and_pay_once(self):
        wb = make_wb()
        add_claim(wb, "Q7", [Part("Q7-1", "Q7", "验证费", 5000,
                                  candidates=("甲试点",), self_funds=3000,
                                  object_key="甲试点", purpose="技术验证")])
        wb.review("Q7")
        wb.approve("Q7", {"Q7-1": "甲试点"})
        entry = wb.pay("Q7", on=D, entry_id="E1")
        self.assertEqual(len(entry.allocations), 1)
        self.assertEqual(entry.allocations[0].scope_key, "甲试点")  # 唯一实际归属
        with self.assertRaises(WorkbenchError):
            wb.pay("Q7", on=D, entry_id="E2")  # 不得重复支付

    def test_dual_caliber_counts_once(self):
        wb = make_wb()
        add_claim(wb, "Q8", [Part("Q8-1", "Q8", "验证费", 5000,
                                  candidates=("甲试点",), self_funds=3000,
                                  object_key="甲试点", purpose="技术验证")])
        wb.review("Q8")
        wb.approve("Q8", {"Q8-1": "甲试点"})
        wb.pay("Q8", on=D, entry_id="E1")
        self.assertEqual(wb._usage("甲试点").paid, 5000)
        self.assertEqual(wb._usage("示范方向").paid, 5000)  # 两个口径都满足
        paid_total = sum(a.amount for e in wb.ledger if e.kind == "paid"
                         for a in e.allocations)
        self.assertEqual(paid_total, 5000)  # 台账只计一次

    def test_reserve_restricted_to_listed_project(self):
        wb = make_wb()
        add_claim(wb, "Q9", [Part("Q9-1", "Q9", "验证费", 5000,
                                  candidates=("甲试点",), self_funds=3000,
                                  object_key="甲试点", purpose="技术验证")])
        (p,) = wb.review("Q9")
        self.assertEqual(wb._bucket_for("甲试点"), "通用额度")  # 非列名不得用保留款
        self.assertTrue(any("专设" in w for o in p.options for w in o.warnings))

    def test_beneficiary_cap_binds(self):
        wb = make_wb()
        wb.add_rule(Rule("cap-ben", RuleKind.CAP, Scope.BENEFICIARY, "甲试点",
                         ("A1",), "受益人累计限6千", amount=6000))
        add_claim(wb, "Q10", [Part("Q10-1", "Q10", "验证费", 7000,
                                   candidates=("甲试点",), self_funds=9000,
                                   object_key="甲试点", purpose="技术验证")])
        (p,) = wb.review("Q10")
        self.assertEqual(p.status, PartStatus.NO_ROUTE)
        self.assertTrue(any("受益人" in r for r in p.options[0].reasons))

    def test_target_is_soft_warning(self):
        wb = make_wb()
        wb.add_rule(Rule("tgt", RuleKind.TARGET, Scope.USE_GROUP, "示范方向",
                         ("A2",), "示范方向目标4千", amount=4000,
                         purposes=("技术验证",)))
        add_claim(wb, "Q11", [Part("Q11-1", "Q11", "验证费", 5000,
                                   candidates=("甲试点",), self_funds=3000,
                                   object_key="甲试点", purpose="技术验证")])
        (p,) = wb.review("Q11")
        self.assertEqual(p.status, PartStatus.PAYABLE)  # 超目标不阻付
        self.assertTrue(any("目标数" in w for o in p.options for w in o.warnings))

    def test_transaction_cap(self):
        wb = make_wb()
        wb.add_rule(Rule("cap-tx", RuleKind.CAP, Scope.TRANSACTION, "单笔",
                         ("A1",), "单笔限4千", amount=4000))
        add_claim(wb, "Q13", [Part("Q13-1", "Q13", "验证费", 5000,
                                   candidates=("甲试点",), self_funds=3000,
                                   object_key="甲试点", purpose="技术验证")])
        (p,) = wb.review("Q13")
        self.assertEqual(p.status, PartStatus.NO_ROUTE)
        self.assertTrue(any("单笔" in r for r in p.options[0].reasons))


class NoteAndRecoveryTest(unittest.TestCase):
    def _approved_setup(self):
        wb = make_wb()
        wb.ingest_ledger(LedgerEntry("E1", "paid", D, "已付验证费", "QX", [
            Allocation("通用额度", "甲试点", "技术验证", 4000, "甲试点",
                       ("示范方向",), None)]))
        add_claim(wb, "QX", [Part("QX-1", "QX", "后续验证费", 6000,
                                  candidates=("甲试点",), self_funds=9000,
                                  object_key="甲试点", purpose="技术验证")])
        wb.review("QX")
        wb.approve("QX", {"QX-1": "甲试点"})
        return wb

    def test_tighten_freezes_pending_and_reviews_paid(self):
        wb = self._approved_setup()
        effect = wb.apply_note(
            Term("N1", 1, D, "补充说明", "甲试点限7千；技术验证费暂停。"),
            [Rule("cap-jia-2", RuleKind.CAP, Scope.PROJECT, "甲试点",
                  ("N1",), "甲试点限7千", amount=7000, overrides="cap-jia"),
             Rule("exc-x", RuleKind.EXCLUSION, Scope.PROJECT, "暂停",
                  ("N1",), "技术验证费暂停支付", purposes=("技术验证",),
                  projects=("甲试点",))],
            on=D)
        self.assertIn("QX-1", effect.frozen)          # 可能超限的待付先冻结
        self.assertEqual(len(effect.review_orders), 1)  # 已付部分发复核
        fz = next(f for f in wb.freezes.values() if f.target_part == "QX-1")
        self.assertTrue(fz.active)
        with self.assertRaises(WorkbenchError):
            wb.pay("QX", on=D, entry_id="E9")          # 冻结中不得支付

    def test_recovery_restores_quota_and_releases_freeze(self):
        wb = self._approved_setup()
        wb.apply_note(
            Term("N1", 1, D, "补充说明", "甲试点限7千。"),
            [Rule("cap-jia-2", RuleKind.CAP, Scope.PROJECT, "甲试点",
                  ("N1",), "甲试点限7千", amount=7000, overrides="cap-jia")],
            on=D)
        fz = next(f for f in wb.freezes.values() if f.target_part == "QX-1")
        self.assertTrue(fz.active)
        wb.post_recovery("R1", reverses="E1", amount=4000, claim_id="QX",
                         on=D, basis=("N1",), text="退回已付款")
        self.assertFalse(fz.active)                    # 确已满足 → 解除
        self.assertEqual(wb._usage("甲试点").paid, 0)  # 回到实际占用额度

    def test_eligibility_freeze_not_released_by_recovery(self):
        wb = self._approved_setup()
        fz = wb.freeze_part("QX-1", 6000, "资格待裁定", ("N1",), kind="ELIGIBILITY")
        wb.post_recovery("R1", reverses="E1", amount=4000, claim_id="QX",
                         on=D, basis=("N1",), text="退回已付款")
        self.assertTrue(fz.active)  # 资格类冻结不因回补解除
        wb.release_freeze(fz.freeze_id, "裁定到位")
        self.assertFalse(fz.active)

    def test_loosen_note_reopens_rejected_part(self):
        wb = make_wb()
        add_claim(wb, "Q12", [Part("Q12-1", "Q12", "运维费", 5000,
                                   candidates=("甲试点",), self_funds=3000,
                                   object_key="甲试点", purpose="常规运维")])
        (p,) = wb.review("Q12")
        self.assertEqual(p.status, PartStatus.REJECTED)
        wb.apply_note(
            Term("N2", 1, D, "补充说明", "放开运维限制。"),
            [Rule("lift-om", RuleKind.LIFT, Scope.GLOBAL, "常规运维",
                  ("N2",), "解除运维排除", overrides="exc-om")],
            on=D)
        self.assertEqual(p.status, PartStatus.PAYABLE)  # 放开后重审成立

    def test_ruling_keeps_rationale(self):
        wb = make_wb()
        ruling = wb.rule_on_dispute("承包方邮件：运维可走研发",
                                    "不采纳", "与层级1条款冲突", on=D)
        self.assertIn("层级", ruling.rationale)


class ScenarioTest(unittest.TestCase):
    """用仓库内置虚构资料跑完整时间线，核对关键结果。"""

    @classmethod
    def setUpClass(cls):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cls.wb = run_demo()
        cls.output = buf.getvalue()

    def test_final_balance(self):
        bal = self.wb.balance()
        self.assertEqual(bal["通用额度"]["可用"], 36000)
        self.assertEqual(bal["研发保留款"]["可用"], 0)
        caps = {c["口径"]: c for c in bal["封顶占用"]}
        self.assertEqual(caps["示范方向"]["已付"], 26000)
        self.assertEqual(caps["青芽关联方"]["余量"], 8000)

    def test_c5_has_no_route_with_reasons(self):
        part = self.wb.parts["C5-1"]
        self.assertEqual(part.status, PartStatus.NO_ROUTE)
        self.assertEqual(len(part.options), 2)
        self.assertTrue(all(not o.feasible for o in part.options))

    def test_review_order_closed_by_recovery(self):
        order = next(iter(self.wb.review_orders.values()))
        self.assertEqual(order.status, "CLOSED")
        self.assertEqual(self.wb.parts["C3-2"].paid_amount, 0)

    def test_trace_shows_whole_chain(self):
        trace = self.wb.trace("C3")
        for marker in ("依据", "复核", "回补", "L3", "L4", "L5"):
            self.assertIn(marker, trace)


if __name__ == "__main__":
    unittest.main()
