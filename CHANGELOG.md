# Changelog

本项目为网文创作数据 Dashboard（Postgres + FastAPI + ingest + React）。  
开发进度跟踪以 `docs/DESIGN-webnovel-dashboard.md` §20 功能清单为准。

## [Phase 2 · F-P2-03] — 2026-08-24

**事件·股票：分拆/并购三形态成本随链引擎**（DESIGN §19.6）

- `app/core/stock_cost.py`：纯函数 `split_position / cash_share_position / cash_merger` + DB 写入 `apply_merger`。
  - 形态1 纯换股/分拆：旧成本按**新股数占比**摊到新持仓（如 UTC→CARR/OTIS/RTX 1:0.5:1）。
  - 形态2 换股+现金：股票腿成本全额随链、现金入余额（如 MVL→30USD+0.7452DIS）。
  - 形态3 纯现金：持仓归 0、现金入余额、不记损益。
- 写新 `holding_event` 批次 + 结清旧行 + 现金 `ledger_entry`；幂等（date+新公司重复应用跳过）。
- `tests/test_stock_cost.py` 7 用例（复现 UTC 分拆 / 2‑for‑1 / MVL‑DIS / 纯现金 / 幂等）；全量 295 passed。
- 说明：prose 事件文件解析、FIFO 卖出/分红底座(F-P2-02)、账户/币种解析另做。

---

## [Phase 1 · P1 收尾] — 2026-08-24

Phase 1 P1 全部功能落地 ✅（DESIGN §20 F-P1-01..10）。本阶段关键交付：

- **F-P1-05 公司图谱外部 API①②**
  - API① 公司导入（`POST /graph/companies/import`，公司图谱页按钮触发）——拉外部 `/public/companies`，只增不减 upsert 公司实体 + 股权 `holds` 边。
  - API② 用工成本（见 F-P1-10）。
- **F-P1-10 用工成本·加薪规则（API②）**
  - 本地基准三表 `labor_wage_benchmark / labor_cpi_growth / labor_tax_benchmark` + `ingest labor-baseline`。
  - 成本公式 `app/core/labor_cost.py`：Level 调整 / 逐年 CPI 增幅 / 外包系数 / 晋升 5% 级 / 各 office 税率公式（含比利时十三薪、双倍假期、英国学徒税、日本 3 月奖金等隐藏项，只在后台）。
  - 岗位导入 `app/ingest/importers/positions.py`：`GET /public/positions` → 逐岗位成本 → 每公司 `finance_entry(company, expense)` 落账。
  - 「加薪规则/用工成本」屏（规则可视化 + 拉岗位计算 + 结果表）。
- **F-P1-08 统一搜索（LLM + agentic RAG）**
  - `search_index` pgvector(4096) + 15 类语义表条目提取器 + `ingest search-index` 全量索引（4768 行已建）。
  - `GET /api/v1/search`：embed → 余弦 top-k → LLM 装配 → serve 后处理（剥推理/复述）；不幻觉（无数据答「资料未提供」）；omxl 未起 503 降级。
  - 前端「搜索」屏（仅渲染最终答案）。
  - 实测问答：祖母去世=1947年、皮克斯三期投资 3000万USD/40% 股权、比利时1982投资金融年薪=1010595 BEF。
- **F-P1-07 财务收支真实库验收**
  - 修复 finance_entry 空库（旧数据早于 `_mirror_to_finance`）：新增 `backfill_finance_entries` + `ingest finance-backfill`，把既有 income_stream/家庭支出幂等回填 → finance_entry 1485 行。
  - `/finance-entries` 端点返回真数据（含实体/类别/金额/币种），财务收支屏可正常浏览。
- **工程/依赖**
  - importer 底座统一 `app/ingest/importers/_client.py`（`_api_root`/`login`/`load`，API①② 共用，避免分支冲突）。
  - requirements 增 `httpx`、`pyyaml`、`pgvector`。
  - 全量测试 288 项通过；前端 `vite build` 通过。