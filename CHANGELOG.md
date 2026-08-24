# Changelog

本项目为网文创作数据 Dashboard（Postgres + FastAPI + ingest + React）。  
开发进度跟踪以 `docs/DESIGN-webnovel-dashboard.md` §20 功能清单为准。

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