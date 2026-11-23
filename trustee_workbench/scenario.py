"""用虚构资料演示受托人付款审核工作台。

资料：data/terms.json（条款）、claims.json（申请）、ledger.json（上期台账）、
proofs.json（合同及凭据补件）。全部金额为整数元。

时间线：
  10-01  T1/T2/T3 生效，受托人登记解释
  10-08  C1 审核；10-09 上期实付 L1 对账
  10-12  C2 审核；10-13 承诺 L2 对账
  10-14  C3 审核（回访服务受阻）；10-16 L3 实付出现对账差异
  10-18  T4 补充说明生效：收紧限额、排除代维、收紧口径
  10-19  C5 双归属审核：均不可行
  10-20  P3 补件认定代维；L4 回补归位；C3 余下设备验证费签批实付
  10-21  T5 争议邮件裁定；C4 口径改列与运维询问
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .model import (
    Allocation,
    Caliber,
    Claim,
    LedgerEntry,
    Part,
    Rule,
    RuleKind,
    Scope,
    Term,
)
from .workbench import GENERAL_BUCKET, Workbench

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _load(name: str) -> list[dict]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _show_parts(wb: Workbench, claim_id: str) -> None:
    for p in wb.review(claim_id):
        print(f"    部件 {p.part_id} 「{p.label}」{p.amount}元 → {p.status.value}"
              + (f"，可付{p.payable}元" if p.payable and p.payable != p.amount else ""))
        for b in p.basis:
            print(f"      依据：{b}")
        for m in p.missing:
            print(f"      待补：{m}")
        for o in p.options:
            mark = "✓" if o.feasible else "✗"
            print(f"      归属候选 {mark} {o.scope_key}")
            for r in o.reasons:
                print(f"          {r}")
            for w in o.warnings:
                print(f"          （提示）{w}")


def run_demo() -> Workbench:
    terms = {t["id"]: t for t in _load("terms.json")}
    claims = {c["id"]: c for c in _load("claims.json")}
    ledger = {e["id"]: e for e in _load("ledger.json")}
    proofs = {p["id"]: p for p in _load("proofs.json")}

    wb = Workbench("（虚构）绿色验证专项募集", total=90000)
    print("=" * 72)
    print(f"{wb.plan_name} · 受托人付款审核工作台")
    print("=" * 72)

    # ---------------------------------------------------------- 条款与解释
    print("\n【一】登记条款原文与受托人解释（10-01）")
    wb.add_term(Term("T1", 1, _d("2026-10-01"), "发行说明书", terms["T1"]["text"]))
    wb.add_term(Term("T2", 2, _d("2026-10-01"), "附件A", terms["T2"]["text"], refs=("T3",)))
    wb.add_term(Term("T3", 2, _d("2026-10-01"), "附件B", terms["T3"]["text"]))
    for pid, row in proofs.items():
        wb.add_proof(pid, _d(row["received"]), row["contract_scope"])

    wb.add_rule(Rule("cap-qy-pilot", RuleKind.CAP, Scope.PROJECT, "青芽试点",
                     ("T3",), "青芽本季试点限2万（T2转引附件B）", amount=20000))
    wb.add_rule(Rule("cap-hq-rd", RuleKind.CAP, Scope.PROJECT, "汉桥研发",
                     ("T3",), "汉桥研发限3万", amount=30000))
    wb.add_rule(Rule("cap-lq-pilot", RuleKind.CAP, Scope.PROJECT, "蓝桥试点",
                     ("T3",), "蓝桥试点限2万", amount=20000))
    wb.add_rule(Rule("cap-demo", RuleKind.CAP, Scope.USE_GROUP, "示范方向",
                     ("T3",), "青芽及蓝桥同属示范方向，累计至多3万", amount=30000))
    wb.add_rule(Rule("cap-qy-ben", RuleKind.CAP, Scope.BENEFICIARY, "青芽关联方",
                     ("T1",), "青芽及实际同一受益人专项费用累计最多3万", amount=30000))
    wb.add_rule(Rule("res-rd", RuleKind.RESERVE, Scope.GLOBAL, "研发保留款",
                     ("T2",), "研发专设保留款2万，只用于列名研发项目",
                     amount=20000, projects=("汉桥研发",)))
    wb.add_rule(Rule("ratio-pilot", RuleKind.SELF_RATIO, Scope.PROJECT, "试点共担",
                     ("T2",), "每个试点项目自筹至少三成",
                     ratio=(3, 10), projects=("青芽试点", "蓝桥试点")))
    wb.add_rule(Rule("exc-om", RuleKind.EXCLUSION, Scope.GLOBAL, "常规运维",
                     ("T1",), "不得用于常规运维（含认定的日常代维）",
                     purposes=("常规运维", "日常代维")))
    wb.add_rule(Rule("exc-pass", RuleKind.EXCLUSION, Scope.GLOBAL, "借用费用",
                     ("T1", "P1"), "配套地场费实为借用/过手费用，非募集用途",
                     purposes=("借用费用",)))
    wb.add_caliber(Caliber("示范方向", ("青芽试点", "蓝桥试点"), (),
                           ("T3",), "青芽及蓝桥同属示范方向"))
    wb.determine_beneficiary("青芽试点", "青芽关联方", ("T1",), "青芽为说明书列名主体")
    wb.determine_beneficiary("青芒", "青芽关联方", ("P1", "P4"),
                             "青芒实际拥有设备并参与收益分配，认定为实际同一受益人")

    # ---------------------------------------------------------- C1 与 L1
    print("\n【二】C1 审核（10-08）：发票名目与实质、试点共担比例")
    c1 = claims["C1"]
    wb.submit(Claim("C1", _d(c1["date"]), c1["splittable"], c1["invoice_id"],
                    c1["text"], c1["total"]), [
        Part("C1-1", "C1", "仪器验证", 20000, purpose_hint="技术验证",
             candidates=("青芽试点",), evidence=("P1",), self_funds=8000,
             object_key="青芽试点", purpose="技术验证"),
        Part("C1-2", "C1", "演示共建（发票列作配套地场服务）", 10000,
             evidence=("P1",), object_key="青芽试点", purpose="借用费用"),
    ])
    _show_parts(wb, "C1")
    l1 = ledger["L1"]
    wb.ingest_ledger(LedgerEntry("L1", "paid", _d(l1["date"]), l1["text"], "C1",
                                 [Allocation(GENERAL_BUCKET, "青芽试点", "技术验证", 16000,
                                             "青芽关联方", ("示范方向",), "C1-1")]))
    print("    对账：L1 实付16,000元 ≤ 可付上限18,666元，与审核结论一致（原账保留）")

    # ---------------------------------------------------------- C2 与 L2
    print("\n【三】C2 审核（10-12）：相近名目不并案，研发保留款列名动用")
    c2 = claims["C2"]
    wb.submit(Claim("C2", _d(c2["date"]), c2["splittable"], c2["invoice_id"],
                    c2["text"], c2["total"]), [
        Part("C2-1", "C2", "汉桥专用试验设备", 28000, purpose_hint="技术验证",
             candidates=("汉桥研发",), evidence=("P2",),
             object_key="汉桥研发", purpose="技术验证"),
    ])
    _show_parts(wb, "C2")
    l2 = ledger["L2"]
    wb.ingest_ledger(LedgerEntry("L2", "commitment", _d(l2["date"]), l2["text"], "C2", [
        Allocation("研发保留款", "汉桥研发", "技术验证", 20000, "汉桥研发", (), "C2-1"),
        Allocation(GENERAL_BUCKET, "汉桥研发", "技术验证", 8000, "汉桥研发", (), "C2-1"),
    ]))
    print("    对账：L2 承诺28,000元（保留款20,000＋通用8,000），与审核结论一致")

    # ---------------------------------------------------------- C3 与 L3
    print("\n【四】C3 审核（10-14）：同一凭据份额分别核算，性质不明部分受阻")
    c3 = claims["C3"]
    wb.submit(Claim("C3", _d(c3["date"]), c3["splittable"], c3["invoice_id"],
                    c3["text"], c3["total"]), [
        Part("C3-1", "C3", "设备验证", 10000, purpose_hint="技术验证",
             candidates=("蓝桥试点",), evidence=(), self_funds=6000,
             object_key="蓝桥试点", purpose="技术验证"),
        Part("C3-2", "C3", "回访服务", 4000, evidence=(),
             object_key="蓝桥试点", composition_known=False),
    ])
    _show_parts(wb, "C3")
    l3 = ledger["L3"]
    print("    ↓ 上期外部台账 L3（10-16）仍支付了含回访服务的一万元：")
    wb.ingest_ledger(LedgerEntry("L3", "paid", _d(l3["date"]), l3["text"], "C3", [
        Allocation(GENERAL_BUCKET, "蓝桥试点", "技术验证", 6000,
                   "蓝桥试点", ("示范方向",), "C3-1"),
        Allocation(GENERAL_BUCKET, "蓝桥试点", "回访服务", 4000,
                   "蓝桥试点", ("示范方向",), "C3-2"),
    ]))
    for _, kind, text, _ in wb.events:
        if kind == "对账":
            print(f"    ⚠ {text}")

    # ---------------------------------------------------------- T4 补充说明
    print("\n【五】T4 补充说明生效（10-18）：收紧限额、排除代维、收紧口径")
    t4 = terms["T4"]
    effect = wb.apply_note(
        Term("T4", 1, _d(t4["from"]), t4["source"], t4["text"]),
        [Rule("cap-qy-ben-2", RuleKind.CAP, Scope.BENEFICIARY, "青芽关联方",
              ("T4",), "青芽关联方限额缩到2.4万", amount=24000, overrides="cap-qy-ben"),
         Rule("exc-lq-dw", RuleKind.EXCLUSION, Scope.PROJECT, "蓝桥代维",
              ("T4",), "蓝桥含代维的费用部分不得从任何类别募集款付出（回访服务性质待查）",
              purposes=("日常代维", "回访服务"), projects=("蓝桥试点",))],
        on=_d(t4["from"]))
    wb.restrict_caliber("示范方向", ("技术验证",), ("T4",),
                        "仅常规运维之外的设备验证费可列示范方向")
    for line in effect.lines:
        print(f"    {line}")

    # ---------------------------------------------------------- C5 双归属
    print("\n【六】C5 审核（10-19）：两种合法归属、不可拆分")
    c5 = claims["C5"]
    wb.submit(Claim("C5", _d(c5["date"]), c5["splittable"], c5["invoice_id"],
                    c5["text"], c5["total"]), [
        Part("C5-1", "C5", "研究与试点同用验证设备", 11000, purpose_hint="技术验证",
             candidates=("青芽试点", "青芽研发"), evidence=("P4",),
             object_key="青芽试点", purpose="技术验证"),
    ])
    _show_parts(wb, "C5")
    print("    结论：两种归属均不可行且申请不可拆分 → 整体无法批准，原因如上留痕")

    # ---------------------------------------------------------- P3 与 L4
    print("\n【七】P3 补件（10-20）：回访服务认定为日常代维；L4 回补归位")
    wb.supplement("C3-2", "P3", purpose="日常代维", composition_known=True,
                  note="合同第六款：四千元回访服务实为日常代维，全额列作运维")
    p3 = wb.parts["C3-2"]
    print(f"    C3-2 → {p3.status.value}：{p3.basis[-1]}")
    order = next(o for o in wb.review_orders.values() if o.entry_id == "L3")
    wb.escalate_review(order.order_id, "追补", "P3认定代维，复核转为追补")
    l4 = ledger["L4"]
    wb.post_recovery("L4", reverses="L3", amount=l4["amount"], claim_id="C3",
                     on=_d(l4["date"]), basis=("P3", "T4"), text=l4["text"],
                     part_id="C3-2")
    print(f"    回补4,000元已回到实际占用额度（蓝桥试点/示范方向/{GENERAL_BUCKET}），"
          f"复核指示{order.order_id}关闭")

    # ---------------------------------------------------------- C3 余款签批
    print("\n【八】C3 余下设备验证费签批实付（10-20）：双口径计一次")
    wb.approve("C3", {"C3-1": "蓝桥试点"}, note="构成经P3澄清，独立成立部分按拆分许可处理")
    wb.pay("C3", on=_d("2026-10-20"), entry_id="L5", text="蓝桥设备验证余款")
    p = wb.parts["C3-1"]
    print(f"    L5 实付{p.payable - 0 - 6000}元（该部分应付10,000元，L3已付6,000元），"
          f"口径[{ '+'.join(p.calibers) }]同时满足，台账只计一次")

    # ---------------------------------------------------------- T5 裁定与 C4
    print("\n【九】T5 承包方邮件裁定（10-21）：语义歧义留依据裁定")
    t5 = terms["T5"]
    wb.add_term(Term("T5", 3, _d(t5["from"]), t5["source"], t5["text"]))
    wb.rule_on_dispute(
        subject=f"T5（{t5['source']}）：{t5['text']}",
        decision="不采纳：蓝桥运维不得改走研发，附件B继续适用",
        rationale="T5为效力层级3的承包方邮件，与层级1的T1（不得用于常规运维）、"
                  "层级1的补充说明T4（蓝桥含代维不得从任何类别付出）冲突，"
                  "且无权排除层级2的附件B；依效力层级不予采纳",
        on=_d("2026-10-21"))

    print("\n【十】C4 决定（10-21）：口径改列不另付款；运维询问不可付")
    c4 = claims["C4"]
    wb.submit(Claim("C4", _d(c4["date"]), c4["splittable"], c4["invoice_id"],
                    c4["text"], c4["total"]), [])
    wb.relist_caliber("C4", "蓝桥试点", "示范方向", ("T4",),
                      "设备验证费属可列范围，改列成立")
    print("    C4(a)：蓝桥设备验证维持列示[示范方向]，不另形成支付，台账不重复计数")
    wb.record_determination("C4", "青芒的日常维护是否能报",
                            "不可付：日常维护属常规运维", ("T1",))
    print("    C4(b)：青芒日常维护 → 不可付（依据T1；T5说法已经裁定不采纳）")

    # ---------------------------------------------------------- 期末余额
    print("\n【十一】期末可动用余额与额度占用")
    bal = wb.balance()
    print(f"    总资金：{bal['总资金']:,}元")
    for bucket in (GENERAL_BUCKET, "研发保留款"):
        b = bal[bucket]
        extra = f"，限{ b['限列名项目'] }" if "限列名项目" in b else ""
        print(f"    {bucket}：总额{b['总额']:,}，已付{b['已付']:,}，"
              f"已签未付{b['已签未付']:,}，可用{b['可用']:,}{extra}")
    for c in bal["封顶占用"]:
        print(f"    [{c['层级']}]{c['口径']}：封顶{c['封顶']:,}，已付{c['已付']:,}，"
              f"已签未付{c['已签未付']:,}，余量{c['余量']:,}")

    # ---------------------------------------------------------- 交接追踪
    print("\n【十二】交接追踪（沿一张申请看同一笔钱）")
    for cid in ("C3", "C5"):
        print(wb.trace(cid))
        print()
    return wb


if __name__ == "__main__":
    run_demo()
