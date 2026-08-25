"""HP_CSC 重组链 spec（F-P2-04 · DESIGN §19.6）。

链条（两条入线在 DXC 汇合）：
- HP 线：CPQ(康柏) → HPQ(换股 0.6325) → HPQ+HPE(1:1 分拆) → HPE 派 DXC(0.086)/MFGP(0.137)
  → MFGP 被 OpenText 收购为 OTEX(0.162 + 现金 8.45)
- CSC 线：CSC → CSC+CSRA(1:1 分拆) → CSC 减持 → CSC→DXC(1:1)；CSRA 被 General Dynamics 现金收购
- DXC = HPE源 4,758,186 + CSC源 10,578,800 = 15,336,986 → 减持 6,554,586 → 8,782,400
  → 分拆 PRSP(2 DXC → 1 PRSP) → PRSP 被 Veritas 现金收购 29.05

本 spec 由「锚定数值」手写（数值已核验闭合），非从源文件解析。见 DESIGN §19.6 827 行。

**模型局限（必须知晓）**：`apply_merger form=split` 会整体关旧开新，无法表达「父公司保留+
子公司另计」。用 legs 含旧公司同名腿近似（如 `HPQ→{HPQ,1},{HPE,1}`、`DXC→{DXC,1},{PRSP,0.5}`）
在数值上保持父头寸 + 生成子头寸，但**子成本按股数占比分摊**（非史实「免费 spin-off 子成本≈0」）。
因此本例**只断言股数 + 现金退出**，不断言 PRSP/子成本绝对值。DXC 合并日两源单价不同属正常
成本系差异，非 H2 冲突。

部分数值（CSC 减持 249,500/199,600、DXC 减持 6,554,586）为回测校准非史实，标 `calibrated`。
"""
from __future__ import annotations

# 买入单价（回测校准 / 名义；share/现金断言不依赖成本，仅供成本随链基准）
CPQ_UNIT = round(956_279_577 / 87_474_700, 6)   # ≈10.932（CH: HP/1.CPQ购买股票 总投入 956,279,577）
CSC_UNIT = 25.68                                  # 名义参考（CSC/1.CSC早期.md，不断言）

HP_CSC_DXC = {
    "name": "HP_CSC_DXC",
    # entity_id 由调用方注入（apply_chain/verify_chain 用 chain["entity_id"]）
    "steps": [
        # ---- HP 线 ----
        {"type": "buy", "company": "CPQ", "ticker": "CPQ", "date": "2001-12-31",
         "shares": 87_474_700, "unit_price": CPQ_UNIT, "account_id": "@buy"},
        {"type": "split", "company": "CPQ", "date": "2002-05-03",
         "legs": [{"company": "HPQ", "per_old_share": 0.6325}]},
        {"type": "split", "company": "HPQ", "date": "2015-11-01",
         "legs": [{"company": "HPQ", "per_old_share": 1.0},
                  {"company": "HPE", "per_old_share": 1.0}]},
        # ---- CSC 线 ----
        {"type": "buy", "company": "CSC", "ticker": "CSC", "date": "2015-11-26",
         "shares": 11_027_900, "unit_price": CSC_UNIT, "account_id": "@buy"},
        {"type": "split", "company": "CSC", "date": "2015-11-27",
         "legs": [{"company": "CSC", "per_old_share": 1.0},
                  {"company": "CSRA", "per_old_share": 1.0}]},
        {"type": "sell", "company": "CSC", "date": "2016-12-31", "shares": 249_500,
         "sell_price": 40.0, "account_id": "@cash", "calibrated": True},
        {"type": "sell", "company": "CSC", "date": "2017-03-31", "shares": 199_600,
         "sell_price": 45.0, "account_id": "@cash", "calibrated": True},
        # ---- 汇合 DXC ----
        {"type": "split", "company": "HPE", "date": "2017-04-01",
         "legs": [{"company": "DXC", "per_old_share": 0.086},
                  {"company": "MFGP", "per_old_share": 0.137}]},
        {"type": "split", "company": "CSC", "date": "2017-04-01",
         "legs": [{"company": "DXC", "per_old_share": 1.0}]},
        {"type": "sell", "company": "DXC", "date": "2017-04-01", "shares": 6_554_586,
         "sell_price": 68.0, "account_id": "@cash", "calibrated": True},
        # ---- 现金退出 / 分拆 ----
        {"type": "cash", "company": "CSRA", "date": "2018-04-04",
         "cash_per_share": 41.25, "cash_account_id": "@cash"},
        {"type": "split", "company": "DXC", "date": "2018-05-31",
         "legs": [{"company": "DXC", "per_old_share": 1.0},
                  {"company": "PRSP", "per_old_share": 0.5}]},
        {"type": "cash", "company": "PRSP", "date": "2021-09-10",
         "cash_per_share": 29.05, "cash_account_id": "@cash"},
        {"type": "cash_share", "company": "MFGP", "date": "2023-04-04",
         "legs": [{"company": "OTEX", "per_old_share": 0.162}],
         "cash_per_share": 8.45, "cash_account_id": "@cash"},
    ],
}


def lb(company: str) -> str:
    """现金断言 reason_like 便捷：apply_merger 现金 ledger reason = `并购现金对价·{old_company}…`。"""
    return f"并购现金对价·{company}"


#: verify_chain expected（依 H2 逐行验证；as_of=2025-12-31）
HP_CSC_DXC_EXPECTED = [
    # open 位置
    {"company": "DXC", "shares": 8_782_400, "open": True},
    {"company": "OTEX", "shares": 1_227_944, "open": True},
    # 已现金退出 / 分拆 / 合并 → 应无 open
    {"company": "PRSP", "shares": 0, "open": False},
    {"company": "CSRA", "shares": 0, "open": False},
    {"company": "MFGP", "shares": 0, "open": False},
    {"company": "CSC", "shares": 0, "open": False},
    {"company": "HPE", "shares": 0, "open": False},
    {"company": "CPQ", "shares": 0, "open": False},
    # 现金退出（按 reason 区分公司；kind=investment_income）
    {"ledger_kind": "investment_income", "reason_like": lb("CSRA"), "amount": 454_900_875.0},
    {"ledger_kind": "investment_income", "reason_like": lb("PRSP"), "amount": 127_564_360.0},
    {"ledger_kind": "investment_income", "reason_like": lb("MFGP"), "amount": 64_050_163.45},
]

#: 主链 unasserted 处理：HPQ 为父保留腿，由子链另验证（informational）
HP_CSC_DXC_UNASSERTED = ["HPQ"]

#: 只读 verify 子链（steps=[]，不重复 buy/分割；只复验主链已建头寸）
HP_CSC_HPINC = {
    "name": "HP_CSC_HPINC",
    "steps": [],
    "expected": [
        {"company": "HPQ", "shares": 55_327_747, "open": True},
    ],
}

HP_CSC_HPE_MFGP_OTEX = {
    "name": "HP_CSC_HPE_MFGP_OTEX",
    "steps": [],
    "expected": [
        {"company": "OTEX", "shares": 1_227_944, "open": True},
    ],
}

#: 全部链（供测试/CLI 遍历）
HP_CSC = {
    "chains": [
        (HP_CSC_DXC, HP_CSC_DXC_EXPECTED),
        (HP_CSC_HPINC, HP_CSC_HPINC["expected"]),
        (HP_CSC_HPE_MFGP_OTEX, HP_CSC_HPE_MFGP_OTEX["expected"]),
    ],
}