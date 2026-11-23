"""受托人付款审核工作台 · 引擎。

职责边界：
- 条款原文、合同与凭据补件只读保存；受托人把解释登记为规则/裁定，
  引擎负责核算一致性（封顶、预留、比例、余额）、生成依据链，
  并在补充说明生效、资金回补等时点做连锁处理。
- 引擎不做自然语言猜测：发票名与别称必须凭原文依据登记裁定后才用于判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .model import (
    Allocation,
    AttributionOption,
    BeneficiaryRuling,
    Caliber,
    Claim,
    Freeze,
    LedgerEntry,
    NoteEffect,
    Part,
    PartStatus,
    ReviewOrder,
    Rule,
    RuleKind,
    Ruling,
    Scope,
    Term,
)

GENERAL_BUCKET = "通用额度"


class WorkbenchError(Exception):
    pass


@dataclass
class Usage:
    paid: int = 0          # 实付净额（已扣回补）
    committed: int = 0     # 已签未付承诺
    approved: int = 0      # 已核准待付

    @property
    def total(self) -> int:
        return self.paid + self.committed + self.approved


class Workbench:
    def __init__(self, plan_name: str, total: int):
        self.plan_name = plan_name
        self.total = total
        self.terms: dict[str, Term] = {}
        self.rules: list[Rule] = []
        self.calibers: list[Caliber] = []
        self.beneficiary_rulings: dict[str, BeneficiaryRuling] = {}
        self.claims: dict[str, Claim] = {}
        self.parts: dict[str, Part] = {}
        self.ledger: list[LedgerEntry] = []
        self.freezes: dict[str, Freeze] = {}
        self.review_orders: dict[str, ReviewOrder] = {}
        self.rulings: list[Ruling] = []
        self.proofs: dict[str, tuple[date, str]] = {}
        self.events: list[tuple[int, str, str, dict]] = []  # (序号, 类别, 叙述, 关联)
        self._seq = 0
        self._ids = 0

    # ------------------------------------------------------------------ 登记
    def _nid(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}-{self._ids:03d}"

    def _log(self, kind: str, text: str, **links) -> None:
        self._seq += 1
        self.events.append((self._seq, kind, text, links))

    def add_term(self, term: Term) -> None:
        self.terms[term.term_id] = term
        self._log("条款", f"登记{term.source} {term.term_id}（效力层级{term.rank}，{term.effective}生效）",
                  term=term.term_id)

    def add_rule(self, rule: Rule) -> None:
        for tid in rule.basis:
            if tid not in self.terms and tid not in self.proofs:
                raise WorkbenchError(f"规则 {rule.rule_id} 依据了未登记的条款或补件 {tid}")
        if rule.overrides:
            old = self._rule(rule.overrides)
            old.superseded = True
        self.rules.append(rule)
        self._log("解释", f"[{rule.kind.value}/{rule.scope.value}] {rule.key}: {rule.note}（依据{ '+'.join(rule.basis) }）",
                  rule=rule.rule_id)

    def add_caliber(self, caliber: Caliber) -> None:
        self.calibers.append(caliber)

    def add_proof(self, proof_id: str, received: date, text: str) -> None:
        """登记合同/凭据补件（只读），供依据链引用。"""
        self.proofs[proof_id] = (received, text)

    def restrict_caliber(self, name: str, purposes: tuple[str, ...],
                         basis: tuple[str, ...], note: str) -> None:
        """补充说明收紧披露口径的用途范围。"""
        old = next((c for c in self.calibers if c.name == name), None)
        projects = old.projects if old else ()
        self.calibers = [c for c in self.calibers if c.name != name]
        self.calibers.append(Caliber(name=name, projects=projects, purposes=purposes,
                                     basis=basis, note=note))
        self._log("口径", f"口径[{name}]用途范围收紧为{list(purposes)}：{note}"
                          f"（依据{self._basis_chain(basis)}）")

    def record_determination(self, claim_id: str, subject: str,
                             decision: str, basis: tuple[str, ...]) -> None:
        """记录受托人就申请中某一询问的判定（不直接形成支付）。"""
        self._log("判定", f"{claim_id}：{subject} → {decision}"
                          f"（依据{self._basis_chain(basis)}）", claim=claim_id)

    def determine_beneficiary(self, surface: str, canonical: str,
                              basis: tuple[str, ...], note: str) -> None:
        """登记实际受益人裁定：表面名称归集到 canonical 口径。"""
        self.beneficiary_rulings[surface] = BeneficiaryRuling(surface, canonical, basis, note)
        self._log("裁定", f"受益人归集：{surface} → {canonical}（依据{ '+'.join(basis) }）：{note}",
                  beneficiary=canonical)

    def _rule(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.rule_id == rule_id:
                return r
        raise WorkbenchError(f"未登记的规则 {rule_id}")

    def _active_rules(self, kind: RuleKind | None = None) -> list[Rule]:
        return [r for r in self.rules if not r.superseded and (kind is None or r.kind is kind)]

    # ------------------------------------------------------------------ 申请
    def submit(self, claim: Claim, parts: list[Part]) -> None:
        if claim.claim_id in self.claims:
            raise WorkbenchError(f"重复的申请 {claim.claim_id}")
        if sum(p.amount for p in parts) > claim.total:
            raise WorkbenchError(f"{claim.claim_id} 拆分合计超过申请总额")
        self.claims[claim.claim_id] = claim
        for p in parts:
            self.parts[p.part_id] = p
        self._log("申请", f"收到 {claim.claim_id}（{claim.invoice_id}，{claim.total}元，"
                          f"{'允许拆分' if claim.splittable else '不可拆分'}），拆分{len(parts)}部分",
                  claim=claim.claim_id)

    # ------------------------------------------------------------------ 审核
    def review(self, claim_id: str) -> list[Part]:
        """逐部分判定：对象→用途→排除→条件→比例→归属可行性。"""
        claim = self.claims[claim_id]
        parts = [p for p in self.parts.values() if p.claim_id == claim_id]
        for part in parts:
            if part.status in (PartStatus.APPROVED, PartStatus.PAID):
                continue
            self._review_part(part)
        # 拆分许可：不允许拆分时，任一部分不成立则整体暂缓
        not_ok = [p for p in parts if p.status not in (PartStatus.PAYABLE,)]
        if not claim.splittable and not_ok:
            for p in parts:
                if p.status == PartStatus.PAYABLE:
                    p.status = PartStatus.BLOCKED
                    p.missing.append("申请不可拆分，须待其余部分成立")
                    p.basis.append("付款请求不允许拆分：独立成立部分随同整体暂缓")
        self._log("审核", f"{claim_id} 审核完毕：" + "；".join(
            f"{p.part_id}={p.status.value}" for p in parts), claim=claim_id)
        return parts

    def _review_part(self, part: Part) -> None:
        part.missing.clear()
        part.options.clear()
        claim = self.claims[part.claim_id]
        # 1) 实际对象与受益人：只凭登记的裁定与补件，不按名称相近猜测
        if part.object_key is None:
            part.status = PartStatus.BLOCKED
            part.missing.append("实际对象身份")
            part.basis.append("发票名目未凭原文登记裁定，实际对象不明 → 受阻待补证")
            return
        part.beneficiary = self._canonical_beneficiary(part)
        # 2) 用途认定与排除项
        if part.purpose is None:
            if not part.composition_known:
                part.status = PartStatus.BLOCKED
                part.missing.append("费用构成")
                part.basis.append("费用构成未载明，实际用途无法认定 → 受阻待补证")
                return
            part.status = PartStatus.BLOCKED
            part.missing.append("用途认定")
            return
        exclusion = self._find_exclusion(part)
        if exclusion:
            part.status = PartStatus.REJECTED
            part.payable = 0
            part.basis.append(
                f"排除项：{exclusion.note}（依据{self._basis_chain(exclusion.basis)}）")
            return
        # 3) 适用时点
        if not part.timing_known:
            part.status = PartStatus.BLOCKED
            part.missing.append("适用时点")
            part.basis.append("适用时点不明 → 受阻待补证")
            return
        # 4) 自筹比例（试点共担）：单一归属候选时在部分层面核算；
        #    多种合法归属时，比例问题记入各候选的可行性，不整体阻断
        part.payable = part.amount
        ratio_rule = self._self_ratio_for(part)
        if ratio_rule and len(part.candidates) <= 1:
            if part.self_funds is None:
                part.status = PartStatus.BLOCKED
                part.missing.append("自筹证明")
                part.basis.append(
                    f"试点共担要求自筹至少{ratio_rule.ratio[0]}/{ratio_rule.ratio[1]}"
                    f"（依据{self._basis_chain(ratio_rule.basis)}），自筹未证明 → 受阻待补证")
                return
            num, den = ratio_rule.ratio
            cap_pay = part.self_funds * (den - num) // num
            if part.amount > cap_pay:
                part.payable = cap_pay
                part.basis.append(
                    f"自筹{part.self_funds}元，募集可承担上限{cap_pay}元（自筹≥{num}/{den}，"
                    f"依据{self._basis_chain(ratio_rule.basis)}）；超出{part.amount - cap_pay}元不予支付")
        # 5) 归属候选可行性（按剩余应付核算）
        remaining = part.payable - part.paid_amount
        for scope_key in (part.candidates or (part.object_key,)):
            part.options.append(self._evaluate_option(part, scope_key, amount=remaining))
        if not any(o.feasible for o in part.options):
            part.status = PartStatus.NO_ROUTE
            part.basis.append("全部合法归属均不可行 → 无法批准")
            return
        part.status = PartStatus.PAYABLE
        # 满足的披露口径（可多条，台账只计一次）
        part.calibers = self._calibers_for(part.object_key, part.purpose)

    def _canonical_beneficiary(self, part: Part) -> str:
        for surface in (part.object_key, part.label):
            r = self.beneficiary_rulings.get(surface)
            if r:
                return r.canonical
        return part.object_key or ""

    def _find_exclusion(self, part: Part) -> Rule | None:
        for r in self._active_rules(RuleKind.EXCLUSION):
            if r.purposes and part.purpose not in r.purposes:
                continue
            if r.projects and part.object_key not in r.projects:
                continue
            return r
        return None

    def _self_ratio_for(self, part: Part) -> Rule | None:
        for r in self._active_rules(RuleKind.SELF_RATIO):
            if r.purposes and part.purpose in r.purposes:
                return r
            if r.projects and part.object_key in r.projects:
                return r
        return None

    def _calibers_for(self, scope_key: str | None, purpose: str | None) -> list[str]:
        out = []
        for c in self.calibers:
            if c.projects and scope_key not in c.projects:
                continue
            if c.purposes and purpose not in c.purposes:
                continue
            out.append(c.name)
        return out

    def _basis_chain(self, term_ids: tuple[str, ...]) -> str:
        """把依据渲染成含转引的链条，如 T2(附件A)→转引T3(附件B)。"""
        segs = []
        for tid in term_ids:
            t = self.terms.get(tid)
            if t is None:
                if tid in self.proofs:
                    segs.append(f"{tid}(合同/凭据补件)")
                else:
                    segs.append(tid)
                continue
            seg = f"{t.term_id}({t.source})"
            if t.refs:
                seg += "→转引" + ",".join(t.refs)
            segs.append(seg)
        return "；".join(segs)

    # ------------------------------------------------------------ 额度核算
    def _usage(self, scope_key: str, purposes: tuple[str, ...] = ()) -> Usage:
        u = Usage()
        is_caliber = any(c.name == scope_key for c in self.calibers)
        for e in self.ledger:
            sign = -1 if e.kind == "refund" else 1
            for a in e.allocations:
                if is_caliber:
                    if scope_key not in a.calibers:
                        continue
                elif a.scope_key != scope_key:
                    continue
                if purposes and a.purpose not in purposes:
                    continue
                if e.kind in ("paid", "refund"):
                    u.paid += sign * a.amount
                elif e.kind == "commitment":
                    u.committed += sign * a.amount
        for p in self.parts.values():
            if p.status == PartStatus.APPROVED and p.attribution == scope_key:
                u.approved += p.payable - p.paid_amount
        return u

    def _beneficiary_usage(self, canonical: str) -> Usage:
        u = Usage()
        for e in self.ledger:
            sign = -1 if e.kind == "refund" else 1
            for a in e.allocations:
                if a.beneficiary != canonical:
                    continue
                if e.kind in ("paid", "refund"):
                    u.paid += sign * a.amount
                elif e.kind == "commitment":
                    u.committed += sign * a.amount
        for p in self.parts.values():
            if p.status == PartStatus.APPROVED and p.beneficiary == canonical:
                u.approved += p.payable - p.paid_amount
        return u

    def _bucket_usage(self, bucket: str) -> Usage:
        u = Usage()
        for e in self.ledger:
            sign = -1 if e.kind == "refund" else 1
            for a in e.allocations:
                if a.bucket != bucket:
                    continue
                if e.kind in ("paid", "refund"):
                    u.paid += sign * a.amount
                elif e.kind == "commitment":
                    u.committed += sign * a.amount
        return u

    def _reserve_rules(self) -> list[Rule]:
        return self._active_rules(RuleKind.RESERVE)

    def _evaluate_option(self, part: Part, scope_key: str,
                         amount: int | None = None,
                         exclude_self: bool = False) -> AttributionOption:
        """检查某候选归属：封顶、口径组、受益人限额、预留限制、桶余额、锁死。

        exclude_self 用于已核准部分的复核（支付前、解冻前）：
        该部分自身已计入占用，不得重复计算。
        """
        reasons: list[str] = []
        warnings: list[str] = []
        if amount is None:
            amount = part.payable
        own = 0
        if exclude_self and part.status is PartStatus.APPROVED:
            own = part.payable - part.paid_amount
        # 试点共担：该候选为试点项目而自筹未证明时，本归属待补证
        for r in self._active_rules(RuleKind.SELF_RATIO):
            if r.projects and scope_key in r.projects and part.self_funds is None:
                reasons.append(
                    f"若列{scope_key}：自筹未证明，本归属待补证"
                    f"（依据{self._basis_chain(r.basis)}）")
        # 项目/用途组封顶
        for cap in self._active_rules(RuleKind.CAP):
            if cap.scope is Scope.PROJECT and cap.key != scope_key:
                continue
            if cap.scope is Scope.USE_GROUP and cap.key not in self._calibers_for(scope_key, part.purpose):
                continue
            if cap.scope is Scope.BENEFICIARY:
                continue  # 下方单独处理
            if cap.purposes and part.purpose not in cap.purposes:
                continue
            u = self._usage(cap.key, cap.purposes)
            if exclude_self and part.attribution == cap.key:
                u.approved = max(0, u.approved - own)
            room = cap.amount - u.total
            if amount > room:
                basis = self._basis_chain(cap.basis)
                if cap.amount - u.paid >= amount:
                    reasons.append(
                        f"锁死已签未付：{cap.key}封顶{cap.amount}元，已付{u.paid}元，"
                        f"本笔{amount}元后余量{cap.amount - u.paid - amount}元 < "
                        f"已签未付{u.committed + u.approved}元（依据{basis}）")
                else:
                    reasons.append(
                        f"超{cap.key}封顶{cap.amount}元：已付{u.paid}元，"
                        f"余量{room}元 < 本笔{amount}元（依据{basis}）")
        # 受益人限额
        ben = self._canonical_beneficiary(part)
        for cap in self._active_rules(RuleKind.CAP):
            if cap.scope is not Scope.BENEFICIARY or cap.key != ben:
                continue
            u = self._beneficiary_usage(ben)
            if exclude_self and part.beneficiary == ben:
                u.approved = max(0, u.approved - own)
            room = cap.amount - u.total
            if amount > room:
                reasons.append(
                    f"超最终受益人限额：{ben}累计上限{cap.amount}元，已发生{u.total}元，"
                    f"余量{room}元 < 本笔{amount}元（依据{self._basis_chain(cap.basis)}）")
        # 预留额：本归属是否可动用
        for res in self._reserve_rules():
            if res.projects and scope_key in res.projects:
                continue  # 列名项目可动用
            warnings.append(
                f"{res.key}为列名项目{list(res.projects)}专设，{scope_key}不在列，"
                f"本笔只能使用{GENERAL_BUCKET}（依据{self._basis_chain(res.basis)}）")
        # 资金桶余额（预留额之外的通用额度受已付+已签未付约束）
        general_total = self.total - sum(r.amount for r in self._reserve_rules())
        gu = self._bucket_usage(GENERAL_BUCKET)
        if amount > general_total - gu.total:
            reasons.append(
                f"{GENERAL_BUCKET}不足：可用{general_total - gu.total}元 < 本笔{amount}元"
                f"（总资金{self.total}元－预留{self.total - general_total}元－已付{gu.paid}元－已签未付{gu.committed}元）")
        # 目标数（软性，只提示）
        for tgt in self._active_rules(RuleKind.TARGET):
            if tgt.scope is Scope.USE_GROUP and tgt.key not in self._calibers_for(scope_key, part.purpose):
                continue
            u = self._usage(tgt.key, tgt.purposes)
            if u.total + amount > tgt.amount:
                warnings.append(
                    f"超目标数（软性，不阻付）：{tgt.key}目标{tgt.amount}元，"
                    f"本笔后累计{u.total + amount}元（依据{self._basis_chain(tgt.basis)}）")
        return AttributionOption(scope_key, feasible=not reasons,
                                 reasons=reasons, warnings=warnings)

    # ------------------------------------------------------------ 补证
    def supplement(self, part_id: str, proof_id: str, *,
                   object_key: str | None = None, purpose: str | None = None,
                   composition_known: bool = False, timing_known: bool = False,
                   self_funds: int | None = None, note: str = "") -> Part:
        """补证到位：更新受阻部分并重新审核。"""
        part = self.parts[part_id]
        if object_key is not None:
            part.object_key = object_key
        if purpose is not None:
            part.purpose = purpose
        if composition_known:
            part.composition_known = True
        if timing_known:
            part.timing_known = True
        if self_funds is not None:
            part.self_funds = self_funds
        part.evidence = part.evidence + (proof_id,)
        part.basis.append(f"补件{proof_id}到达：{note}")
        self._log("补证", f"{part_id} 收到{proof_id}：{note}", claim=part.claim_id, part=part_id)
        # 整单重审：不可拆分时，其余部分的暂缓可能随之解除
        self.review(part.claim_id)
        return part

    # ------------------------------------------------------------ 签批与实付
    def approve(self, claim_id: str, choices: dict[str, str], note: str = "") -> None:
        """签批：choices 为 part_id → 选定的唯一归属。"""
        claim = self.claims[claim_id]
        parts = [p for p in self.parts.values() if p.claim_id == claim_id]
        if not claim.splittable:
            bad = [p for p in parts if p.status != PartStatus.PAYABLE]
            if bad:
                raise WorkbenchError(
                    f"{claim_id} 不允许拆分且存在不成立部分：{[p.part_id for p in bad]}")
        approved_any = False
        for part in parts:
            if part.status != PartStatus.PAYABLE:
                continue
            choice = choices.get(part.part_id)
            if choice is None:
                raise WorkbenchError(f"{part.part_id} 未选定归属")
            remaining = part.payable - part.paid_amount
            # 签批时按剩余应付重新核算，防止审核后状态变化
            option = self._evaluate_option(part, choice, amount=remaining)
            if not option.feasible:
                raise WorkbenchError(
                    f"{part.part_id} 选定归属 {choice} 不可行：{'；'.join(option.reasons)}")
            part.attribution = choice
            part.status = PartStatus.APPROVED
            approved_any = True
            part.basis.append(f"签批归属 {choice}" + (f"；签批说明：{note}" if note else ""))
            self._log("核准", f"{part.part_id} 核准 {remaining}元，归属 {choice}",
                      claim=claim_id, part=part.part_id)
        if not approved_any:
            raise WorkbenchError(f"{claim_id} 没有可核准的部分")

    def pay(self, claim_id: str, on: date, entry_id: str, text: str = "") -> LedgerEntry:
        """实付：为已核准且未冻结的部分生成台账分录（每部分唯一归属）。"""
        allocations: list[Allocation] = []
        for part in self.parts.values():
            if part.claim_id != claim_id or part.status != PartStatus.APPROVED:
                continue
            if self._active_freeze(part.part_id):
                raise WorkbenchError(f"{part.part_id} 存在有效冻结，不得支付")
            remaining = part.payable - part.paid_amount
            if remaining <= 0:
                continue
            option = self._evaluate_option(part, part.attribution,
                                           amount=remaining, exclude_self=True)
            if not option.feasible:
                raise WorkbenchError(f"{part.part_id} 支付前复核不再可行：{option.reasons}")
            bucket = self._bucket_for(part.attribution)
            allocations.append(Allocation(
                bucket=bucket, scope_key=part.attribution, purpose=part.purpose,
                amount=remaining, beneficiary=part.beneficiary or "",
                calibers=tuple(part.calibers), part_id=part.part_id,
                basis=tuple(b for b in part.basis)))
            part.paid_amount += remaining
            part.status = PartStatus.PAID
        if not allocations:
            raise WorkbenchError(f"{claim_id} 没有可支付的已核准部分")
        entry = LedgerEntry(entry_id=entry_id, kind="paid", date=on,
                            text=text or f"{claim_id} 实付", claim_id=claim_id,
                            allocations=allocations)
        self.ledger.append(entry)
        for a in allocations:
            cal = f"，口径[{ '+'.join(a.calibers) }](计一次)" if a.calibers else ""
            self._log("实付", f"{entry_id}：{a.scope_key} {a.amount}元（{a.bucket}）{cal}",
                      claim=claim_id, entry=entry_id)
        return entry

    def _bucket_for(self, scope_key: str) -> str:
        for res in self._reserve_rules():
            if res.projects and scope_key in res.projects:
                return res.key
        return GENERAL_BUCKET

    def _active_freeze(self, part_id: str) -> Freeze | None:
        for f in self.freezes.values():
            if f.target_part == part_id and f.active:
                return f
        return None

    def freeze_part(self, part_id: str, amount: int, reason: str,
                    basis: tuple[str, ...], kind: str = "ELIGIBILITY") -> Freeze:
        """受托人手动冻结（资格类须待裁定或补证解除）。"""
        fz = Freeze(freeze_id=self._nid("FZ"), target_part=part_id, amount=amount,
                    reason=reason, basis=basis, kind=kind)
        self.freezes[fz.freeze_id] = fz
        self._log("冻结", f"{part_id} 冻结{amount}元：{reason}", part=part_id)
        return fz

    def release_freeze(self, freeze_id: str, note: str) -> None:
        fz = self.freezes[freeze_id]
        fz.active = False
        fz.release_note = note
        self._log("解冻", f"{fz.target_part} 冻结解除：{note}", part=fz.target_part)

    def _release_satisfied_freezes(self, source: str) -> None:
        """解除确已满足的冻结（仅额度类；资格类须待裁定或补证）。"""
        for fz in self.freezes.values():
            if not fz.active or fz.kind != "CAP":
                continue
            part = self.parts[fz.target_part]
            remaining = part.payable - part.paid_amount
            option = self._evaluate_option(part, part.attribution,
                                           amount=remaining, exclude_self=True)
            if option.feasible:
                fz.active = False
                fz.release_note = f"{source}后额度满足，冻结解除"
                self._log("解冻", f"{fz.target_part} 冻结解除：{fz.release_note}",
                          part=fz.target_part)

    # ------------------------------------------------------------ 台账与承诺
    def ingest_ledger(self, entry: LedgerEntry) -> None:
        """接收上期外部台账：原账与原签解释保留；与审核结论对账。"""
        entry.external = True
        self.ledger.append(entry)
        sign = -1 if entry.kind == "refund" else 1
        for a in entry.allocations:
            if a.part_id and a.part_id in self.parts:
                self.parts[a.part_id].paid_amount += sign * a.amount
                part = self.parts[a.part_id]
                if entry.kind == "paid" and part.status in (
                        PartStatus.BLOCKED, PartStatus.REJECTED, PartStatus.NO_ROUTE):
                    self._log("对账", f"差异：{entry.entry_id} 支付了审核未通过的部分 "
                                     f"{a.part_id}（{part.status.value}）{a.amount}元，列入复核线索",
                              entry=entry.entry_id, part=a.part_id)
        self._log("台账", f"接收外部分录 {entry.entry_id}（{entry.kind}，"
                          f"{sum(a.amount for a in entry.allocations)}元）：{entry.text}",
                  entry=entry.entry_id, claim=entry.claim_id)

    # ------------------------------------------------------------ 补充说明
    def apply_note(self, term: Term, effects: list[Rule], on: date) -> NoteEffect:
        """补充说明生效：收紧或放开范围，并做连锁处理。

        - 仍能履行的承诺保留；履行受阻的标出待重议；
        - 可能超限的已核准待付部分先冻结；
        - 已付部分留原账与当时依据，对受影响部分发复核/追补指示。
        """
        self.add_term(term)
        effect = NoteEffect()
        for rule in effects:
            self.add_rule(rule)
            effect.lines.append(f"登记：{rule.note}（依据{self._basis_chain(rule.basis)}）")
        # 1) 已签承诺：仍能履行的保留
        for e in self.ledger:
            if e.kind != "commitment":
                continue
            problems = self._commitment_problems(e)
            if problems:
                effect.at_risk.append(e.entry_id)
                effect.lines.append(f"承诺 {e.entry_id} 履行受阻，需重议：{'；'.join(problems)}")
            else:
                effect.retained.append(e.entry_id)
                effect.lines.append(f"承诺 {e.entry_id} 仍能履行 → 保留")
        # 2) 已核准待付：可能超限的部分先冻结
        for part in self.parts.values():
            if part.status != PartStatus.APPROVED:
                continue
            option = self._evaluate_option(part, part.attribution)
            if not option.feasible:
                fz = Freeze(freeze_id=self._nid("FZ"), target_part=part.part_id,
                            amount=part.payable - part.paid_amount,
                            reason="；".join(option.reasons),
                            basis=tuple(r.rule_id for r in effects), kind="CAP")
                self.freezes[fz.freeze_id] = fz
                effect.frozen.append(part.part_id)
                effect.lines.append(f"待付 {part.part_id} 可能超限 → 冻结{fz.amount}元：{fz.reason}")
                self._log("冻结", f"{part.part_id} 冻结{fz.amount}元：{fz.reason}",
                          claim=part.claim_id, part=part.part_id)
        # 3) 已付：受新排除项影响的部分发复核/追补指示，原账保留
        for e in self.ledger:
            if e.kind != "paid":
                continue
            for a in e.allocations:
                for rule in effects:
                    if rule.kind is not RuleKind.EXCLUSION:
                        continue
                    if rule.projects and a.scope_key not in rule.projects:
                        continue
                    if rule.purposes and a.purpose not in rule.purposes:
                        continue
                    order = ReviewOrder(
                        order_id=self._nid("RO"), entry_id=e.entry_id, part_id=a.part_id,
                        amount=a.amount, instruction="复核",
                        basis=rule.basis,
                        note=f"{term.term_id}生效后，{a.scope_key}的{a.purpose}{a.amount}元"
                             f"落入排除范围（{rule.note}）；原账与当时依据保留，待构成认定")
                    self.review_orders[order.order_id] = order
                    effect.review_orders.append(order.order_id)
                    effect.lines.append(
                        f"已付 {e.entry_id} 中 {a.amount}元 → 发{order.instruction}指示（{order.order_id}）")
                    self._log("复核", f"{order.order_id} 对 {e.entry_id} 的{a.amount}元发复核指示",
                              entry=e.entry_id, order=order.order_id)
        # 4) 在审部分按新范围重审；放开范围时，受阻/不可付部分一并重审
        for part in self.parts.values():
            if part.status in (PartStatus.PENDING, PartStatus.PAYABLE):
                self._review_part(part)
        if any(r.kind is RuleKind.LIFT for r in effects):
            for part in self.parts.values():
                if part.status in (PartStatus.BLOCKED, PartStatus.REJECTED,
                                   PartStatus.NO_ROUTE):
                    self._review_part(part)
        # 5) 解除确已满足的冻结
        self._release_satisfied_freezes(f"说明{term.term_id}生效")
        self._log("说明", f"补充说明 {term.term_id} 生效：保留{len(effect.retained)}、"
                          f"受阻{len(effect.at_risk)}、冻结{len(effect.frozen)}、复核{len(effect.review_orders)}",
                  term=term.term_id)
        return effect

    def _commitment_problems(self, entry: LedgerEntry) -> list[str]:
        problems = []
        for a in entry.allocations:
            for cap in self._active_rules(RuleKind.CAP):
                if cap.scope is Scope.PROJECT and cap.key != a.scope_key:
                    continue
                if cap.scope is Scope.USE_GROUP and cap.key not in a.calibers:
                    continue
                if cap.scope is Scope.BENEFICIARY and cap.key != a.beneficiary:
                    continue
                if cap.purposes and a.purpose not in cap.purposes:
                    continue
                u = self._usage(cap.key, cap.purposes)
                if u.total > cap.amount:
                    problems.append(f"{cap.key}累计{u.total}元超封顶{cap.amount}元")
            for res in self._reserve_rules():
                if a.bucket == res.key and res.projects and a.scope_key not in res.projects:
                    problems.append(f"{a.scope_key}不在{res.key}列名项目内")
        return problems

    # ------------------------------------------------------------ 裁定
    def escalate_review(self, order_id: str, instruction: str, note: str) -> None:
        """补件或认定到位后，把复核指示升级为追补等进一步指示。"""
        order = self.review_orders[order_id]
        order.instruction = instruction
        order.note += f"；{note}"
        self._log("复核", f"{order_id} 升级为{instruction}：{note}", order=order_id)

    def rule_on_dispute(self, subject: str, decision: str, rationale: str,
                        on: date) -> Ruling:
        """对语义歧义/争议文本的裁定，依据留痕。"""
        ruling = Ruling(ruling_id=self._nid("RL"), subject=subject,
                        decision=decision, rationale=rationale, date=on)
        self.rulings.append(ruling)
        self._log("裁定", f"{ruling.ruling_id}：{decision}（依据：{rationale}）",
                  ruling=ruling.ruling_id)
        return ruling

    # ------------------------------------------------------------ 回补
    def post_recovery(self, entry_id: str, reverses: str, amount: int,
                      claim_id: str | None, on: date, basis: tuple[str, ...],
                      text: str, part_id: str | None = None) -> LedgerEntry:
        """补回资金：回到实际被占用的额度，并解除确已满足的冻结。

        part_id 指定冲回哪一部分；缺省时按分录顺序冲减。
        """
        original = next((e for e in self.ledger if e.entry_id == reverses), None)
        if original is None:
            raise WorkbenchError(f"找不到被冲回的分录 {reverses}")
        allocations = []
        remaining = amount
        for a in original.allocations:
            if part_id is not None and a.part_id != part_id:
                continue
            take = min(a.amount, remaining)
            if take <= 0:
                continue
            allocations.append(Allocation(
                bucket=a.bucket, scope_key=a.scope_key, purpose=a.purpose,
                amount=take, beneficiary=a.beneficiary, calibers=a.calibers,
                part_id=a.part_id, basis=basis))
            remaining -= take
            if a.part_id and a.part_id in self.parts:
                self.parts[a.part_id].paid_amount -= take
        if remaining > 0:
            raise WorkbenchError(f"回补{amount}元超过 {reverses} 的可冲回金额")
        entry = LedgerEntry(entry_id=entry_id, kind="refund", date=on, text=text,
                            claim_id=claim_id, allocations=allocations, reverses=reverses)
        self.ledger.append(entry)
        self._log("回补", f"{entry_id}：冲回 {reverses} 的{amount}元，"
                          f"回到{ '+'.join(sorted({a.scope_key for a in allocations})) }等实际占用额度",
                  entry=entry_id, claim=claim_id)
        # 关闭对应复核/追补指示
        for order in self.review_orders.values():
            if order.entry_id == reverses and order.status == "OPEN" and order.amount <= amount:
                order.status = "CLOSED"
                order.outcome = f"经{entry_id}回补{amount}元，指示关闭"
                self._log("复核", f"{order.order_id} 关闭：{order.outcome}", order=order.order_id)
        # 解除确已满足的冻结（仅额度类；资格类须待裁定或补证）
        self._release_satisfied_freezes(f"回补{entry_id}")
        return entry

    # ------------------------------------------------------------ 口径改列
    def relist_caliber(self, claim_id: str, scope_key: str, caliber: str,
                       basis: tuple[str, ...], note: str) -> None:
        """把既有款项列入某披露口径：只改列示，不另形成支付，不重复计数。"""
        cal = next((c for c in self.calibers if c.name == caliber), None)
        if cal is None:
            raise WorkbenchError(f"未登记的披露口径 {caliber}")
        touched = []
        for e in self.ledger:
            if e.kind != "paid":
                continue
            for a in e.allocations:
                if a.scope_key != scope_key or caliber in a.calibers:
                    continue
                if cal.purposes and a.purpose not in cal.purposes:
                    raise WorkbenchError(
                        f"{a.purpose} 不符合口径 {caliber} 的用途要求（{cal.note}）")
                a.calibers = a.calibers + (caliber,)
                touched.append(f"{e.entry_id}:{a.amount}元")
        self._log("口径", f"{claim_id}：{scope_key} 改列[{caliber}]，涉及{ '、'.join(touched) or '无' }；"
                          f"不另形成支付，台账不重复计数。{note}",
                  claim=claim_id)

    # ------------------------------------------------------------ 余额
    def balance(self) -> dict:
        reserves = {r.key: r for r in self._reserve_rules()}
        general_total = self.total - sum(r.amount for r in reserves.values())
        gu = self._bucket_usage(GENERAL_BUCKET)
        out = {
            "总资金": self.total,
            GENERAL_BUCKET: {
                "总额": general_total, "已付": gu.paid,
                "已签未付": gu.committed, "可用": general_total - gu.total,
            },
        }
        for key, r in reserves.items():
            u = self._bucket_usage(key)
            out[key] = {"总额": r.amount, "已付": u.paid,
                        "已签未付": u.committed, "可用": r.amount - u.total,
                        "限列名项目": list(r.projects)}
        caps = []
        for cap in self._active_rules(RuleKind.CAP):
            u = (self._beneficiary_usage(cap.key) if cap.scope is Scope.BENEFICIARY
                 else self._usage(cap.key, cap.purposes))
            caps.append({"层级": cap.scope.value, "口径": cap.key, "封顶": cap.amount,
                         "已付": u.paid, "已签未付": u.committed + u.approved,
                         "余量": cap.amount - u.total})
        out["封顶占用"] = caps
        return out

    # ------------------------------------------------------------ 交接追踪
    def trace(self, claim_id: str) -> str:
        """沿一张申请还原同一笔钱的：原文解释→核准→实付→复核→回补。"""
        lines = [f"══ 交接追踪 {claim_id} ══"]
        claim = self.claims.get(claim_id)
        if claim:
            lines.append(f"申请：{claim.date} {claim.invoice_id} 总额{claim.total}元 "
                         f"({'允许拆分' if claim.splittable else '不可拆分'})")
            lines.append(f"原文：{claim.text}")
        for p in [p for p in self.parts.values() if p.claim_id == claim_id]:
            lines.append(f"  部件 {p.part_id} 「{p.label}」{p.amount}元 → {p.status.value}"
                         + (f"，归属 {p.attribution}" if p.attribution else ""))
            for b in p.basis:
                lines.append(f"    依据：{b}")
            for o in p.options:
                mark = "✓" if o.feasible else "✗"
                lines.append(f"    归属候选 {mark} {o.scope_key}"
                             + ("" if o.feasible else "：" + "；".join(o.reasons)))
        for e in self.ledger:
            if e.claim_id != claim_id:
                continue
            kind = {"paid": "实付", "commitment": "承诺", "refund": "回补"}[e.kind]
            rev = f"，冲回{e.reverses}" if e.reverses else ""
            lines.append(f"  台账 {e.entry_id}（{kind}，{e.date}{rev}）：{e.text}")
            for a in e.allocations:
                cal = f" 口径[{ '+'.join(a.calibers) }]" if a.calibers else ""
                lines.append(f"    {a.bucket} → {a.scope_key} {a.amount}元（{a.purpose}）{cal}")
        for order in self.review_orders.values():
            entry = next((e for e in self.ledger if e.entry_id == order.entry_id), None)
            if entry and entry.claim_id == claim_id:
                lines.append(f"  复核 {order.order_id}（{order.status}）：{order.note}"
                             + (f" → {order.outcome}" if order.outcome else ""))
        for fz in self.freezes.values():
            part = self.parts.get(fz.target_part)
            if part and part.claim_id == claim_id:
                state = f"已解除（{fz.release_note}）" if not fz.active else "生效中"
                lines.append(f"  冻结 {fz.freeze_id}（{state}）：{fz.reason}")
        return "\n".join(lines)
