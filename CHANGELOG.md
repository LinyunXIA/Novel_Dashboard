# Changelog

本项目为网文创作数据 Dashboard（Postgres + FastAPI + ingest + React）。  
开发进度跟踪以 `docs/DESIGN-webnovel-dashboard.md` §20 功能清单为准。

---

## [Audit Round 3+4+5 · #151-#179] — 2026-08-26

**三轮/四轮/五轮对照审计修复批**（跟踪索引 #157/#172/#179；DESIGN §21.7–§21.9）

- **三轮（PR #158，#151-#156）**：Dashboard 渲染崩溃死代码、CLI 快照动态上限收尾
  （calendar_years 全链路）、overlay 三端点补重算、§14.2 两端点补齐
  （snapshots/{date}、source-files 单版本内容）、英国学徒税 levy 公式、docs 回写。
- **F-P2-07 导出落地（PR #159）**：`app/export/` md 六节档案 + csv 五 scope（RFC4180）
  + reportlab PDF 报告（CJK 字体内嵌）；`/api/v1/exports`×3；「导入状态」屏导出中心；
  F-P2-08 移入 Phase 3（F-P3-01）。
- **四轮（PR #173 P0 + PR #174 P1-P3，#160-#171）**：PDF 中文 CJK 字体注册、换汇正向
  零汇率防御、bank 节标题币种配对识别、return_table 复合年化封盘、事件关联同币种铁律、
  .gitignore data/** 重写、event_stock 日期归一、前端 useFetch r.ok+防乱序、ingest 卫生九项、
  core/API 健壮性十五项、测试缺口 G 系列、§21.9 回写。
- **五轮（本批，#175-#178）**：换汇反向负汇率穿透修复 + available_fx_pairs 过滤非法行；
  closed 关池账户全通道拒新流水（movie link/stock 动作/_require_open_account 防线 +
  前端过滤）；ui_ops 五端点异常收窄（业务族 422/409、其余 500 通用文案）；
  NotificationsBanner ack 校验；Timeline save 失败保留编辑 + 行级按钮 busy；
  年份输入上限动态化（calMax prop）；Health 屏错误上屏；G6 missing_rates 实质断言；
  return_table 零条告警达主链路（ingest_report 落库）；死变量/未用导入清理。

最终验证：pytest **531 passed**；vite build 通过。

---

## [Phase 2 · F-P2-06] — 2026-08-25

**文件 diff 回退：版本 diff → UI 决策「采纳新版本」/「回退」（DB+磁盘复原）**（DESIGN §11）

- `app/core/versioning.py`：
  - `list_tracked`：被跟踪文件 + 当前磁盘 vs is_current 状态（new/unchanged/changed）+ 近期版本。
  - `file_diff`：unified diff（difflib）磁盘 vs is_current / 任意两历史版本（含 +/- 行数）。
  - `adopt_current`：采纳新版本 → **复用 `import_all(force_files={该文件})`** 真正重导入该文件
    （`import_all`/`_skip_by_state` 加 `force_files` 参数，命中文件即便 unchanged 也强制导入）
    → 记新版为 is_current + notification(kind='file-updated')。避免"只改 is_current 不落库新记录"的坑。
  - `restore_version`：回退 → §11.3 安全写盘（resolve 下 `is_relative_to(source_dir)` 防越权 +
    原子写 tmp→os.replace）复原 source_dir 文件 + 该版本置 is_current + notification。
- `app/api/source_files.py`：`GET /source-files`、`GET/{vid}/versions`、`GET/{vid}/diff?version_id=`、
  `POST/{vid}/versions`(采纳)、`POST/{vid}/versions/{v2}/restore`(回退)；普通 UI 放行。
- 前端「版本/diff」屏（SourceDiff.jsx）：文件列表 + 状态徽标 + diff 渲染(加减高亮) + 采纳/回退按钮。
- **写盘目标偏离（明示）**：本仓库实际 ingest 直接读 `source_dir`（Design_Folder，gitignored 真数据），
  无独立 input_dir 流 → F-P2-06 回退写回 source_dir，是对 §11.3「写 input_dir」语言的有意偏离（否则磁盘无从复原）。
- 11 新单测（versioning 6 / source_files_api 4 / force-import 1）；全量 **369 passed**。

---

## [Phase 2 · F-P2-05] — 2026-08-25

**时间线/编年史 UI 编辑：overlay 增改删 + 差异/重置回源/以源为最新**（DESIGN §12/§6.4）

- **覆盖层服务层** `app/core/overlay.py`：DB-backed 覆盖层（`user_data_overlay` 权威 + 合并到
  `timeline_event(overlay=True)`）；`create/update/delete/merge/diff/restore/source_as_latest`。
  - **隔离（issue #86）**：用户覆盖行 `source_file=f"overlay:timeline:{key}"`；系统 overlay 行
    （投资/划拨/活期结息 `source_file=NULL`）结构只读，不纳入编辑/差异/重置。
  - key=`{year}:{title}`；定位按 (event_year,title) 列。update 改 title/year → 迁移到新 key。
  - diff 比 event_date/title/note/decade → new/modified(changed_fields)/unchanged。
  - **JSONB 变更检测坑**：payload 须「先 copy 再整对象赋值」（`dict(payload)`），in-place 改不持久化。
- **API** `app/api/timeline.py`：POST/PATCH/DELETE /timeline-events、/overlay/restore、/overlay/source-as-latest、
  /overlay/diff；**合并 GET**（按 key 每行一行、覆盖行优先）；普通 UI 放行（importer 例外）。
  移除 app.py 原只读 GET /timeline-events(+/id) 避免路由冲突。
- **前端**「编年史」屏（Timeline.jsx）：新增/编辑/删除覆盖条目、差异表、重置回源/以源为最新；
  系统行只读徽标、源行「覆盖编辑」、覆盖行 `unchanged` 显示「已同步源」。
- 12 新单测（test_overlay 8 + test_timeline_api 4）；全量 **358 passed**；前端 build 通过。

---

## [Phase 2 · F-P2-04] — 2026-08-25

**HP_CSC 重组链数值导入 → 依 §11.4 + H2 逐行验证**（DESIGN §19.6 / §11.4 / §10）

- **链编排器** `app/core/stock_chain.py`：
  - `apply_chain`：把一段重组链（建仓 → 并购/分拆 → 减持 → 现金退出）编码为事件序列，按 `(date, 写入序)`
    稳定排序驱动 `apply_buy / apply_merger / apply_sell / apply_dividend / apply_passive_uplift`，
    自动 `event_id`(source_file) 幂等可重放，标 `calibrated` 的步聚合到 `calibrated_steps`。
  - `verify_chain`：逐行对账（F-P2-04「依 H2 逐行验证」执行体），持股断言（open 求和比对，容差 1 股）、
    现金断言（kind + reason/note LIKE 求和）；只读不改库；unasserted 标 informational。
- **HP_CSC 链 spec** `app/core/hp_csc_chain.py`：`HP_CSC_DXC` 主链 14 步（CPQ→HPQ 0.6325、HPQ→HPInc+HPE 1:1、
  HPE→DXC 0.086/MFGP 0.137、CSC→CSC+CSRA 1:1、CSRA 现金 41.25、CSC→DXC 1:1、DXC 减持 6,554,586、
  DXC→PRSP 2:1、PRSP 现金 29.05、MFGP→OTEX 0.162+现金 8.45）——as_of=2025 闭合到 DXC 8,782,400 / OTEX 1,227,944
  / 三笔现金（CSRA 454,900,875 / PRSP 127,564,360 / MFGP 64,050,163.45）；`HP_CSC_HPINC`/`HP_CSC_HPE_MFGP_OTEX`
  只读子链复验 HPQ / OTEX（不重复建仓）。
- **H2 stock 分支** `health.check_stock_h2`：R1 同公司 >1 个 buy 源成本极差 >3× → warn（只看 buy，
  排除 split/acquire-* cost-chaining 双源，如 DXC 合并日不误报）；R2 同日同公司 buy/sell 多源单价不一 → crit。
- **§11.4 stock 冲突** `conflict.check_stock_event_conflict`：跨文件同 (company,date,event_type) 金额/股数
  不符 → hard-block；**同 source_file 重导入不算冲突**（幂等 upsert 处理）；接入 `events-stock` 按文件 gate。
- **模型局限（明示）**：`apply_merger form=split` 无法「父保留+子另计」——用 legs 同名腿近似（如
  `HPQ→{HPQ,1},{HPE,1}`）在数值上保持父头寸+生成子头寸，但子成本按占比分摊（非史实「免费 spin-off 子
  成本≈0」）→ 只断言股数+现金，不断言子成本绝对值。链内部分数值（DXC 减持 6,554,586、CSC 减持）为
  回测校准非史实，标 `calibrated`。
- 顺带修复 `apply_merger`：结清旧行改为只关**事件前已存在**的旧行 id（保住同名腿），并让 `_already_applied`
  在提供 `source` 时并入 source_file 判定（避免同天多源都产同公司互相误挡）。
- 21 新单测（chain 7 / hp_csc 5 / health 4 / conflict 5）；全量 **346 passed**；dev 库 `events-stock` 重导
  26 条全部幂等跳过、0 阻塞；health H2 无新增误报。

---

## [Phase 2 · F-P2-02/03 follow-up] — 2026-08-25

**分拆/并购市值漏记修复：`holding_event` 结清窗口化（closed_on 列）**（DESIGN §19.6）

- `holding_event` 加 `closed_on`（结清日，可空）——迁移 `d1e2f3a4b5ca`。
- `stock_cost.apply_merger` 对旧公司由「破坏性 `shares=0`」改为「标 `closed_on=重构日`」：保留股数/成本历史，
  使**分拆/并购前年份**市值正确计入旧公司（此前恒为 0，漏记），重构后年份计入新公司且不重复。
- `stock_wealth.market_value_at` / `portfolio_breakdown` 改 `closed_on` 时间窗求值（`closed_on IS NULL OR closed_on > as_of`）；
  `_open_batches` / `apply_sell` / API `positions` / 健康 `check_holding_value` 均追加 `closed_on IS NULL`（排除已结清行）。
- 残余局限注明于 docstring：`apply_sell` 部分卖出仍递减原 buy 行（非全事件流重放），留待后续。
- `tests/test_stock_cost.py` 结清断言改 `closed_on is not None`；`tests/test_stock_wealth.py` 新增 3 回归
  （预重构年市值=旧 UTX、重构后新三家且排除 UTX、`_open_batches` 排除结清、rebuild 预/重构年 entity 含市值）。
- 全量 **325 passed**；dev 库迁移应用、health 无新增异常。

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