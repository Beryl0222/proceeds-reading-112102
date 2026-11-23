"""受托人付款审核工作台 · 数据模型。

约定：
- 金额一律为整数元。
- 条款原文（Term）只读保存；机器可核算的解释登记为规则（Rule），
  每条规则记录其原文依据（term_id 元组），以便签批前说明"靠什么成立"。
- 同一笔款项只有一种实际资金归属（attribution 唯一），
  但可以同时满足多条披露口径（calibers 可多条，台账只计一次）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date


class Scope(enum.Enum):
    """承诺/限制的适用层级。"""

    GLOBAL = "全局"
    USE_GROUP = "一组用途"
    PROJECT = "项目"
    BENEFICIARY = "最终受益人"
    TRANSACTION = "单笔"


class RuleKind(enum.Enum):
    CAP = "封顶数"
    TARGET = "目标数"      # 软性目标：超出只提示，不阻付
    RESERVE = "预留额"     # 必须保留、限指定对象动用
    SELF_RATIO = "自筹比例"
    EXCLUSION = "排除项"
    LIFT = "解除"          # 放开范围：解除既有规则（overrides 指向被解除者）


class PartStatus(enum.Enum):
    PENDING = "待审核"
    PAYABLE = "可签批"
    BLOCKED = "受阻待补证"
    REJECTED = "不可付"
    NO_ROUTE = "无可行归属"
    APPROVED = "已核准待付"
    PAID = "已实付"


@dataclass(frozen=True)
class Term:
    """条款原文。rank 越小效力越优先。"""

    term_id: str
    rank: int
    effective: date
    source: str
    text: str
    refs: tuple[str, ...] = ()   # 条款间转引


@dataclass
class Rule:
    """受托人对条款的一条可核算解释。

    basis 记录所依据的条款 id；overrides 指向被本规则收紧/替代的旧规则。
    """

    rule_id: str
    kind: RuleKind
    scope: Scope
    key: str                              # 如 "青芽试点" / "青芽关联方" / "示范方向" / "研发保留款"
    basis: tuple[str, ...]
    note: str
    amount: int = 0                       # CAP/TARGET/RESERVE 的金额（元）
    ratio: tuple[int, int] | None = None  # SELF_RATIO: 自筹至少 (分子, 分母)
    purposes: tuple[str, ...] = ()        # 适用用途；空 = 不限用途
    projects: tuple[str, ...] = ()        # RESERVE 可动用的列名项目 / EXCLUSION 限定项目
    effective: date | None = None
    overrides: str | None = None
    superseded: bool = False


@dataclass(frozen=True)
class Caliber:
    """披露口径：满足条件可列示，不形成新的支付。"""

    name: str
    projects: tuple[str, ...]
    purposes: tuple[str, ...]
    basis: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class BeneficiaryRuling:
    """实际受益人裁定：表面名称 → 归集口径。"""

    surface: str
    canonical: str
    basis: tuple[str, ...]   # 证据 id（合同/凭据补件）
    note: str


@dataclass
class Claim:
    claim_id: str
    date: date
    splittable: bool          # 付款请求对拆分的许可
    invoice_id: str
    text: str
    total: int


@dataclass
class AttributionOption:
    """一个候选归属的可行性结论。"""

    scope_key: str
    feasible: bool
    reasons: list[str] = field(default_factory=list)   # 不可行的原因
    warnings: list[str] = field(default_factory=list)  # 提示（如超目标数）


@dataclass
class Part:
    """申请拆分后的最小核算单位。同一部分只有一种实际资金归属。"""

    part_id: str
    claim_id: str
    label: str                          # 发票/合同名目（表面名称）
    amount: int
    purpose_hint: str | None = None     # 合同/凭据给出的用途线索
    candidates: tuple[str, ...] = ()    # 合法归属候选（项目/用途分类）
    evidence: tuple[str, ...] = ()      # 支撑补件 id
    self_funds: int | None = None       # 已证明的自筹（试点共担用）；None = 未证明
    composition_known: bool = True      # 费用构成是否载明
    timing_known: bool = True           # 适用时点是否明确
    # ---- 审核结论 ----
    object_key: str | None = None       # 判定的实际对象
    beneficiary: str | None = None      # 判定的实际受益人（归集口径）
    purpose: str | None = None          # 认定的实际用途
    status: PartStatus = PartStatus.PENDING
    payable: int = 0                    # 可付金额（比例/排除后）
    missing: list[str] = field(default_factory=list)   # 受阻所缺补证
    basis: list[str] = field(default_factory=list)     # 依据链（解释）
    options: list[AttributionOption] = field(default_factory=list)
    attribution: str | None = None      # 签批选定的唯一归属
    calibers: list[str] = field(default_factory=list)  # 满足的披露口径
    paid_amount: int = 0                # 已实付（含净回补）


@dataclass
class Allocation:
    """台账分录中的一笔资金占用。"""

    bucket: str                 # 资金桶："通用额度" / "研发保留款"
    scope_key: str              # 实际归属（项目分类）
    purpose: str
    amount: int
    beneficiary: str = ""
    calibers: tuple[str, ...] = ()
    part_id: str | None = None
    basis: tuple[str, ...] = ()


@dataclass
class LedgerEntry:
    entry_id: str
    kind: str                   # paid / commitment / refund
    date: date
    text: str                   # 原签解释（外部台账原样保留）
    claim_id: str | None = None
    allocations: list[Allocation] = field(default_factory=list)
    reverses: str | None = None
    external: bool = False      # 上期外部台账：原账保留


@dataclass
class Freeze:
    freeze_id: str
    target_part: str
    amount: int
    reason: str
    basis: tuple[str, ...]
    kind: str = "CAP"           # CAP=额度类（回补可解） / ELIGIBILITY=资格类（需裁定或补证）
    active: bool = True
    release_note: str | None = None


@dataclass
class ReviewOrder:
    """对已付资金受影响部分发出的复核/追补指示。原账与当时依据保留。"""

    order_id: str
    entry_id: str
    part_id: str | None
    amount: int
    instruction: str            # 复核 / 追补
    basis: tuple[str, ...]
    note: str
    status: str = "OPEN"        # OPEN / CLOSED
    outcome: str | None = None


@dataclass
class Ruling:
    """受托人对语义歧义/争议文本的裁定，依据留痕。"""

    ruling_id: str
    subject: str
    decision: str
    rationale: str
    date: date


@dataclass
class NoteEffect:
    """补充说明生效后的连锁处理结果。"""

    retained: list[str] = field(default_factory=list)       # 仍能履行而保留的承诺
    at_risk: list[str] = field(default_factory=list)        # 履行受阻、需重议的承诺
    frozen: list[str] = field(default_factory=list)         # 新冻结的待付部分
    review_orders: list[str] = field(default_factory=list)  # 对已付部分发出的复核/追补
    lines: list[str] = field(default_factory=list)          # 叙述行
