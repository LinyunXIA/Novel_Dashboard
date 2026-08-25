# Changelog

本项目为网文创作数据 Dashboard（Postgres + FastAPI + ingest + React）。  
开发进度跟踪以 `docs/DESIGN-webnovel-dashboard.md` §20 功能清单为准。

---

## [Phase 2 · F-P2-02] — 2026-08-25

**事件·股票：holding_event(batch) + FIFO 成本 + 分红/卖出结算 + 被动抬升，持仓市值并入总资产**（DESIGN §19.6）

- **解析/导入**：`stock_event` 表（镜像 movie_event，`ingest events-stock` 幂等 upsert）+ `event_stock` 解析器实装（best-effort，USD Style A 流水表：虎牙/哔哩/快手，金额即万美金；快手/香港/英国 万港元/万英镑 与 收购/ 子目录留二期/ F-P2-03-04）。dev 库入库 21 条。
- **成本结算引擎** `app/core/stock_cost.py` 追加：
  - `apply_buy`：买批次(holding_event batch) + ledger `kind='expense'` 现金移出（非 investment，避免与投资池 `pool_in_transit` 重复计数）。
  - `apply_sell`（FIFO）：从最早 open batch 扣成本，超卖 422；写 sell 行 + ledger `income`(本金)+`investment_income`(盈亏) 两笔共=套现现金；非破坏双写。
  - `apply_dividend`：每股×现持仓 → ledger `investment_income`，不落 holding（不污染 open batch）。
  - `apply_passive_uplift`：仅写 `pseudo` 行(占比标记, shares=0 不构成 open)更新 pct，不写 ledger。
  - 统一 `event_id`(source_file) 幂等 + ledger note `股票事件·{id}` 打标（可撤销）。
- **持仓市值入总资产**：新 `app/core/stock_wealth.py`（market_value_at / portfolio_breakdown，Σ open batch shares×unit_price，只进 entity/family 域不进 account 域）接入 `rebuild_snapshots` 与 `calendar.snapshot_as_of`；DESIGN §19.6「总资产=现金+专款池+股票市值」第 3 项首次落地。
- **API + 前端**：`GET/POST /stock-events(+/events/positions/associate/buy/sell/dividend/passive-uplift)`（写操作 `rebuild_snapshots` 刷新）+「股票事件」屏（持仓表/待关联 buy 关联/手动动作）。
- **健康校验**：新增 **H-STOCK** 规则（shares>0 但 unit_price 缺失 → warn；持仓引用缺失实体 → crit）。
- `tests/test_stock_position_fifo.py`(10) + `tests/test_stock_wealth.py`(6) + `tests/test_stock_api.py`(7) + 复跑既有；全量 **322 passed**。

---

## [Phase 2 · F-P2-01] — 2026-08-24

## [Phase 2 · F-P2-01] — 2026-08-24

**事件·电影：导入 + 不关联 + 同币种 UI 手动关联**（DESIGN §19.6）

- `movie_event` 表 + `event_movie` 解析器（best-effort 正则，8 部：泰坦尼克投资90M/本金90M@1998-09/分红376.74M 全中）+ `ingest events-movie` 导入。
- 移除解析 Phase 2 早 return：事件类别真正进到 parser（event_stock 后由 F-P2-02 实装）。
- API `GET/POST /movie-events(+/link/unlink)`：关联写 投资出/本金返还/分红 ledger（幂等），解关联只清标记不动历史账。
- 前端「电影事件」屏（未关联列表 + 同币种账户关联 + 已关联/解关联）。
- `tests/test_movie_event.py`（解析/upsert/phase2 放行/link 幂等）；全量 299 passed。

---

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