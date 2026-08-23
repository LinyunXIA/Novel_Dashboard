# DESIGN — 网文创作数据 Dashboard

对应 [PRD-webnovel-dashboard.md](PRD-webnovel-dashboard.md) 的技术设计文档。目标：把 PRD 落成可实现的架构、数据模型(DDL)、解析器、增量重算算法、文件变更流与 API 契约。

> 版本：1.0（草案） · 三环境：dev / test / prod（三个独立本地 Postgres 库）

---

## 1. 总体架构与分层

### 1.1 分层
```
┌─────────────────────────────────────────────────────────┐
│ Web 前端（React + Vite）                                   │
│  10 屏：Dashboard/全局日历/搜索/图谱(人物·公司)/收益曲线/  │
│  财务收支/编年史/diff 决策/健康/导入状态                    │
└──────────────────────────┬──────────────────────────────┘
                           │ REST (FastAPI)
┌──────────────────────────▼──────────────────────────────┐
│ Serve 层（FastAPI）                                      │
│  查询聚合、as-of 快照读取、健康校验查询、导出、文件决策      │
└──────────────────────────┬──────────────────────────────┘
          读               │                     写（导入/回退）
┌──────────────────────────▼──────────────────────────────┐
│ Postgres 派生索引库（三环境各一）                             │
│  normalized tables + snapshot + recompute_job +           │
│  source_file_version + user_data_overlay                  │
└──────────────────────────┬──────────────────────────────┘
                           ▲
┌──────────────────────────┴──────────────────────────────┐
│ Ingest/Parse → Normalize → Recompute（Python 脚本/ETL）    │
│  读：源 md（只读） + 覆盖层 md（可编辑/仅编年史）              │
│  写：只有 Recompute 写库；只有「回退」写输入/服务器文件夹文件  │
└─────────────────────────────────────────────────────────┘
```

### 1.2 单向原则
- **源 md（Novel 设计源库 `Design_Folder/`）→ Ingest 只读**，任何代码不得写回。
- **覆盖层 md → 可 UI 编辑**（当前限时间线/编年史）。
- **输入文件夹 → 可被 Ingest 读；可被「回退」写**（复原上版内容）。
- **Postgres → 由 Recompute 写入**，前端只读查询（通过 Serve）。

---

## 2. 技术栈选型

| 层 | 选型 | 理由 |
|----|------|------|
| 语言/解析 | Python 3.14（开发时建 `.venv`，当前为空） | 文本解析生态（`markdown-it-py`、`pandas`、`sqlalchemy`） |
| 存储 | 本地 PostgreSQL | 已被采纳；支持 JSONB、事务、并发、SQL 查询与 CHECK 约束 |
| 迁移 | Alembic | DDL 变更可版本化，三环境共用 |
| API | FastAPI + Pydantic | 异步、schema 自动、Swagger |
| ETL | 自研 pipeline + Click 命令 | 代码级触发：`python -m dashboard ingest` |
| 前端 | React + Vite + TypeScript | 组件化；交互（日历拖动、图谱、diff） |
| 图表 | ECharts | 曲线/柱状/环形；graph 模式可兼做图谱 |
| 图谱 | ECharts graph（或 G6） | 人—人/公司关系 |
| 导出 | 后端（markdown 模板、CSV、报告 PDF 用 PyPDF/reportlab） | 仅导出 |

---

## 3. 目录 / 模块结构

```
Novel_Dashboard/
└─ app/
   ├─ config.py              # 环境(dev/test/prod)取库/路径/输入目录
   ├─ db.py                  # SQLAlchemy 引擎/会话；迁移入口
   ├─ ingest/
   │  ├─ main.py             # CLI: python -m app.ingest.main
   │  ├─ detect.py           # 文件→类别(银行/股票/收益表/汇率/人物/时间线；基准/事件 排除)
   │  ├─ parsers/
   │  │  ├─ bank.py          # 银行台账
   │  │  ├─ stock_tx.py      # 股票年度明细
   │  │  ├─ return_table.py  # 收益测算表
   │  │  ├─ fx.py            # 汇率
   │  │  ├─ character.py     # 人物
   │  │  ├─ timeline.py      # 时间线(+覆盖层 merge)
   │  │  ├─ event_movie.py    # 事件·电影（Phase 2 启用，§6.1）
   │  │  └─ event_stock.py    # 事件·股票（Phase 2 启用，§6.1）
   │  ├─ normalize.py        # 统一数据模型
   │  └─ conflict.py         # 导入前冲突检测(§11.4 hard-block)
   ├─ core/
   │  ├─ currency.py         # 多币种折算(基于 exchange_rate)
   │  ├─ leverage.py         # R1-R5 + 杠杆复利口径
   │  ├─ snapshot.py         # 逐年 as-of 快照重建
   │  ├─ recompute.py        # 增量重算(受影响起点)
   │  └─ health.py           # 健康校验规则(§10 导入后全量)
   ├─ model/                 # SQLAlchemy ORM(§6)
   ├─ api/                   # FastAPI routers(§11)
   ├─ export/                # md/CSV/PDF 生成(§12)
   └─ overlay/               # 编年史覆盖层读写
```

**输入目录约定**（各环境配置）：
- `source_dir`：Novel 设计源库（只读）。
- `input_dir`：数据调整员放置待导入文件的目录（新增/更新都先落到这里）。
- `overlay_dir`：用户数据 md 覆盖层（编年史）。

---

## 4. 配置与环境

`config.py` 按 `APP_ENV ∈ {dev,test,prod}` 读取：

```python
ENV = {
  "dev":  dict(dsn="postgresql://…/novel_dev",  source_dir="Design_Folder", input_dir="data/input-dev",   overlay_dir="data/overlay-dev"),
  "test": dict(dsn="postgresql://…/novel_test", source_dir="Design_Folder", input_dir="data/input-test",  overlay_dir="data/overlay-test"),
  "prod": dict(dsn="postgresql://…/novel_prod", source_dir="Design_Folder", input_dir="data/input",       overlay_dir="data/overlay"),
}
```
- 三环境代码同源，仅 DSN 与数据目录（`input_dir`/`overlay_dir`）不同；`input_dir` 各自独立，避免 test 抽样与 dev 全量导入互相污染。
- **本地 LLM（搜索，§18.5）**：`LLM_URL`/`LLM_MODEL`、`EMBED_URL`/`EMBED_MODEL` 走 `omlx-server`（`http://127.0.0.1:8000`，无鉴权），三环境共用同一本地推理，不入 DSN。
- dev/test 均指向真实数据；test 可抽样（如每文件取 2/30 行精度校验）。
- 初始化：`alembic upgrade head && python -m app.ingest.main --full`，无 UI 向导。

---

## 5. 数据模型（Postgres DDL）

### 5.1 主码约定
- 主键 `BIGSERIAL`。
- `as_of_date DATE`：日历粒度（E 全局日历游标）。`as_of_year INT` 可由 `as_of_date` 派生或由 §6.2 默认规则从源数据解析。
- `integration_key`：由 `source_file + 行号/内部序号` 派生，供版本对比与回退定位。
- 除实体表外，所有"行数据"表带 `source_file`（可空·外部 API 实体可为空）、`source_line`、`version_id` 以支持 diff/回退。

### 5.2 DDL（核心表）

```sql
-- 实体（人物 / 公司 / 资产主体）
CREATE TABLE entity (
  id             BIGSERIAL PRIMARY KEY,
  entity_type    TEXT NOT NULL CHECK (entity_type IN ('person','company','asset','family')),
  name           TEXT NOT NULL,
  display_name   TEXT,
  status         TEXT,                            -- 公司状态（仅 company 用；取值开发时定；公司集合只增不减）
  fields         JSONB NOT NULL DEFAULT '{}',   -- 人物/资产的自由字段
  source_file    TEXT,                              -- 可空：外部 API 导入的公司无 source_file
  source_line    INT,
  source         TEXT,                              -- 'file'（来自 md）或 'external-api'（来自外部系统）
  version_id     BIGINT,
  UNIQUE(entity_type, name)
);

-- 账户：主体 × 币种资金池（两级：人物/公司=大账号 × 其下币种池；池可整体关闭）
CREATE TABLE account (
  id          BIGSERIAL PRIMARY KEY,
  entity_id   BIGINT NOT NULL REFERENCES entity(id),
  currency    TEXT NOT NULL,                     -- BEF/LUF/NLG/DKK/SEK/USD/HKD/EUR…
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')),
  closed_on   DATE,                              -- 池关闭日（如 BEF/LUF/NLG=2002-01-01）
  migrate_to_currency TEXT,                      -- 关池后承接币种（如 EUR）
  bank        TEXT,
  UNIQUE(entity_id, currency, bank)
);

-- 初始资产存量（本金/面值/房产/股票债券存档，Phase1；是收益流量的存量基数）
CREATE TABLE initial_asset (
  id          BIGSERIAL PRIMARY KEY,
  entity_id   BIGINT NOT NULL REFERENCES entity(id),
  asset_type  TEXT NOT NULL,                     -- cash / bond / stock / property
  group_key   TEXT,                              -- 股票债券同域打包颗粒（如"丹麦股票债券"）
  currency    TEXT, name TEXT,
  face_value  NUMERIC, pct NUMERIC,
  source_file TEXT, source_line INT
);

-- 收益流模块（Phase1：租/经营性房/祖产债券/开店/薪资；归属 entity、按属地模块拆、逐年入账）
CREATE TABLE income_stream (
  id          BIGSERIAL PRIMARY KEY,
  entity_id   BIGINT NOT NULL REFERENCES entity(id),   -- 归属人物/公司
  stream_type TEXT,                              -- rent / property / security / shop / salary
  group_key   TEXT,                              -- 属地/地域颗粒（股票债券打包挂账用）
  currency    TEXT NOT NULL,
  year        INT  NOT NULL,
  amount      NUMERIC NOT NULL,                  -- 文件写死的金额/税后值，直接入账
  label       TEXT,
  source_file TEXT, source_line INT
);

-- 银行台账逐行流水（余额随行累计）
CREATE TABLE ledger_entry (
  id           BIGSERIAL PRIMARY KEY,
  account_id   BIGINT NOT NULL REFERENCES account(id),
  date         DATE NOT NULL,
  reason       TEXT,
  inflow       NUMERIC,                          -- 收入
  outflow      NUMERIC,                          -- 支出
  balance      NUMERIC,                          -- 余额（schema 存源值；重算校验连续）
  note         TEXT,
  source_file  TEXT, source_line INT, version_id BIGINT
);
CREATE INDEX ix_ledger_acct_date ON ledger_entry(account_id, date);

-- 股票持仓事件（含 batch 维度：每批买入=一个 batch，各自成本，支持 FIFO 卖出、分拆/并购成本传导，§19.6）
CREATE TABLE holding_event (
  id           BIGSERIAL PRIMARY KEY,
  entity_id    BIGINT NOT NULL REFERENCES entity(id),   -- 持仓主体
  company      TEXT NOT NULL,                           -- 标的公司
  ticker       TEXT,
  date         DATE NOT NULL,
  event_type   TEXT,                                    -- buy / sell / split / acquire-cash / acquire-share / pseudo (派生股)
  batch_id     BIGINT,                                  -- 成本批次；FIFO 卖出从最早 batch 扣成本
  shares       NUMERIC,
  unit_price   NUMERIC,                                 -- 该批次成本价（FIFO 用）
  amount       NUMERIC,                            -- 单位：万美金（对应源文件「金额(万美金)」列）
  pct          NUMERIC,
  source_file  TEXT, source_line INT, version_id BIGINT
);

-- 收益测算：国家 × R1-R5 × 年份
CREATE TABLE return_curve (
  id         BIGSERIAL PRIMARY KEY,
  country    TEXT NOT NULL,
  risk_lvl   TEXT NOT NULL CHECK (risk_lvl IN ('R1','R2','R3','R4','R5')),
  year       INT  NOT NULL,
  rate       NUMERIC,                            -- 百分数，如 21.7
  source_file TEXT, source_line INT, version_id BIGINT,
  UNIQUE(country, risk_lvl, year)
);

-- 汇率：货币对 × 年份；year 为 NULL → 基准折算率（常量，不随时间变，如 1EUR=40.3399BEF）；非空 → 逐年表
CREATE TABLE exchange_rate (
  id         BIGSERIAL PRIMARY KEY,
  fx_from    TEXT NOT NULL, fx_to TEXT NOT NULL,
  year       INT,
  rate       NUMERIC,
  source_file TEXT, source_line INT, version_id BIGINT
);

-- 日期解析规则（F）：不完整日期超默认规则时提醒，并由用户补齐
CREATE TABLE date_rule (
  id          BIGSERIAL PRIMARY KEY,
  pattern     TEXT NOT NULL,                      -- 'year-only' / 'year-month初一' / '上旬' / …
  resolve     TEXT NOT NULL,                      -- '12-30' / '月底' / '1日' / …
  note        TEXT,
  UNIQUE(pattern)
);

-- 时间线 / 编年史（含覆盖层 merge 后的生效版本）
CREATE TABLE timeline_event (
  id           BIGSERIAL PRIMARY KEY,
  event_year   INT NOT NULL,
  event_date   DATE,
  title        TEXT NOT NULL,
  note         TEXT,
  decade       TEXT,
  overlay      BOOLEAN NOT NULL DEFAULT FALSE,   -- TRUE=来自覆盖层(UI 编辑)
  source_file  TEXT, source_line INT, version_id BIGINT
);

-- 实体关系（人—人 / 人—公司 / 公司—资产 / 并购换股分拆链）
CREATE TABLE relationship (
  id           BIGSERIAL PRIMARY KEY,
  from_entity_id BIGINT NOT NULL REFERENCES entity(id),
  to_entity_id   BIGINT NOT NULL REFERENCES entity(id),
  rel_type     TEXT NOT NULL,                    -- parent/child/member/holds/acquired/split…
  since_year   INT, until_year INT,
  source_file  TEXT, source_line INT, version_id BIGINT
);

-- 财务收支：始终归属于某个人或某家公司(entity_id 必填，须为 person/company)
-- source='file'（文件导入）或 'ui'（投资等 UI 派生，见 §19 投资功能；kind='investment' / 'investment_income' / 'pool'）
CREATE TABLE finance_entry (
  id          BIGSERIAL PRIMARY KEY,
  entity_id   BIGINT NOT NULL REFERENCES entity(id),
  entity_kind TEXT NOT NULL CHECK (entity_kind IN ('person','company')),  -- 人物或公司
  year        INT  NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('income','expense','investment','investment_income','pool')),
  amount      NUMERIC, currency TEXT,
  label       TEXT,
  source      TEXT DEFAULT 'file',              -- 'file' | 'ui'（派生，§19）
  source_file TEXT, source_line INT, version_id BIGINT
);

-- 投资事件（UI 派生，§19）：一年一「年份+地区」一笔；主体金额细目在 investment_alloc
CREATE TABLE investment (
  id           BIGSERIAL PRIMARY KEY,
  year         INT NOT NULL,
  region       TEXT NOT NULL,                     -- 欧洲/英国/美国/香港/中国（对应 return_curve.country）
  risk_lvl     TEXT NOT NULL CHECK (risk_lvl IN ('R1','R2','R3','R4','R5')),
  start_date   DATE NOT NULL,                     -- 投资发生日（年月日）
  locked       BOOLEAN NOT NULL DEFAULT TRUE,     -- TRUE=已投锁灰；FALSE=解锁重输
  UNIQUE(year, region)                            -- 每「年份+地区」一年一次
);

-- 投资分配：主体 × 币种 × 金额（「全部」= 按 as-of 各币种账户全投）
CREATE TABLE investment_alloc (
  id           BIGSERIAL PRIMARY KEY,
  investment_id BIGINT NOT NULL REFERENCES investment(id) ON DELETE CASCADE,
  entity_id    BIGINT NOT NULL REFERENCES entity(id),
  currency     TEXT NOT NULL,
  amount       NUMERIC NOT NULL,                  -- 该币种投入额（≤ as-of 该币种余额）
  is_all       BOOLEAN NOT NULL DEFAULT FALSE     -- TRUE=「全部」投入该主体该币种
);

-- 时间线/编年史 覆盖层(用户数据 md 承载)(独立于 timeline_event，merge 后进 timeline_event.overlay=True)
CREATE TABLE user_data_overlay (
  id         BIGSERIAL PRIMARY KEY,
  section    TEXT NOT NULL,                       -- 'timeline' 等
  key        TEXT NOT NULL,                       -- 定位覆盖对象
  payload    JSONB NOT NULL,                      -- 用户改动内容
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 逐年 as-of 快照（日历读取用，预计算）
-- 粒度提升为 date 级（E 全局日历游标）；NULL = 该年聚合视图
CREATE TABLE snapshot (
  id          BIGSERIAL PRIMARY KEY,
  as_of_year  INT NOT NULL,                       -- 派生/冗余年份，便于按年聚合
  as_of_date  DATE,                               -- date 级游标；NULL 表示仅年聚合
  scope       TEXT NOT NULL,                      -- 'account:12:BEF' / 'entity:3' / 'family:total'
  value       NUMERIC, currency TEXT
);
-- 年聚合与 date 级分别唯一（避免 NULL 在 UNIQUE 中多发的歧义）
CREATE UNIQUE INDEX ux_snap_year ON snapshot(as_of_year, scope) WHERE as_of_date IS NULL;
CREATE UNIQUE INDEX ux_snap_date ON snapshot(as_of_date, scope) WHERE as_of_date IS NOT NULL;
CREATE INDEX ix_snap_years ON snapshot(as_of_year);

-- 每文件上版基准（diff 与回退 用）
CREATE TABLE source_file_version (
  id          BIGSERIAL PRIMARY KEY,
  file_path   TEXT NOT NULL,
  version     INT  NOT NULL,
  content     TEXT NOT NULL,                      -- 上版完整内容
  captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_current  BOOLEAN,
  UNIQUE(file_path, version)
);

-- 增量重算任务与前端提示
CREATE TABLE recompute_job (
  id          BIGSERIAL PRIMARY KEY,
  start_year  INT,                                -- 受影响起点
  reason      TEXT,                               -- 导入/回退/覆盖层编辑
  files       TEXT[],
  status      TEXT CHECK(status IN('pending','running','done','failed')),
  created_at  TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ
);
CREATE TABLE notification (
  id          BIGSERIAL PRIMARY KEY,
  job_id      BIGINT REFERENCES recompute_job(id),
  kind        TEXT,                               -- recompute-done / file-updated / need-decision
  title       TEXT, message TEXT,
  payload     JSONB,
  read_at     TIMESTAMPTZ,
  created_at  TIMESTAMPTZ DEFAULT now()
);
```

---

## 6. 解析器设计

统一 `parse(path)` → `NormalizedFile`（每个文件一批已归一化记录）。失败不阻塞其他文件，进入 `ingest_report`。

### 6.1 类别识别（detect.py）
按路径 `input_dir 下的相对路径` 匹配：
| 相对路径前缀 | 类别 |
|---|---|
| `经济/银行/` | bank |
| `经济/股票/` | stock_tx |
| `基准/收益表/*测算表` | return_table（R1–R5，仅供投资） |
| `基准/收益表/惠民租房.md` | income_rent（房产租金收益流） |
| `基准/收益表/经营性房产收益.md` | income_property（经营性房产收益流） |
| `基准/收益表/祖产股票债券收益.md` | income_security（祖产债券/股票收益流） |
| `基准/收益表/祖父开店.md` | income_shop（开店收益流） |
| `基准/初始资产/*.md` | initial_asset（存量/本金建档） |
| `基准/薪资/*.md` | salary（逐年薪资收入，取文件税后值） |
| `基准/1974-2001家庭支出.md`（及 CPI 基准） | household_expense（家庭支出，挂 Henri 账户） |
| `基准/汇率/` | fx |
| `人物/` | character |
| `时间线.md` | timeline |
- **阶段项**：`基准/事件/**`（电影/股票等事件素材）**Phase 1 跳过**；**Phase 2 重新启用**（对齐 PRD §2.3 按阶段导入范围与 §19.6）。Phase 1 detect 遇到 `基准/事件/` 直接跳过、不建 `holding_event`；Phase 2 恢复 `event_movie` / `event_stock` 解析器并由数据调整员导入后 UI 同位手动关联账户。
- **模版略不同**：解析器容忍表头/分隔符差异（`\|` 或 `\t`）、单位列缺失；识别失败 → 归类"需人工处理"，不入库。

### 6.2 通用文本工具（normalize.py）
- **数字**：去千分位逗号、`万`/`亿` 单位、`≈`、`≈X.XX` 四舍五入。
- **货币**：`(万)USD/BEF/LUF/NLG/DKK/SEK/HKD/EUR` 后缀识别；无显式则继承所在节币种。
- **日期（统一日历年，无"财年/时间尺"口径）**：`YYYY` / `YYYY-MM-DD`；全系统用**日历历法（1/1–12/31，结算 12-30）**；日历覆盖区间 **1947（最早）–2026（最晚）**。源数据若出现"某财年(6-30 截止)"，须**归一为日历年日期**，不留财年维。`1995-01-01`→as_of_year=1995。
  - **源数据缺失粒度 → 默认规则补全（F，日历与解析共用）**：仅提供年份 → 当年 `12-30`（注明「年初」→`01-01`）；年份+月份 → 该月**月底**（写明「月初」→该月 `1 日`）；「上旬/中旬/下旬」→`1 日/11 日/21 日`。全局日历可精确到年-月-日，但源数据只给到"年或年月"时按此规则补全到日。
  - **超规则处置**：源数据日期无法套用任何已知规则 → 报「需人工」并**提醒 UI 用户**；用户**补充一条 `date_rule`**（`POST /api/v1/date-rules`），后续解析复用该规则。
- **固定 vs 年标记**：行含年份列/日期列 → 年标记；否则 → 固定值（写入 entity.fields 或常量表）。

### 6.3 各解析器要点
- **bank**：按 `## 一、…BEF（祖父）` 分币种节；每节读表列 `日期|理由|收入|支出|余额|备注`；`account_id` 由 `entity × currency × bank` 唯一确定。**余额**：存入源值，并于 normalize 校验连续（见 §9）。
- **stock_tx**：`### 基本信息` 列表 → 常量入 entity.fields；`### 年度明细` 表 → holding_event。拆股/换股链可由 `event_type` 关联出一张 `relationship`（acquired/split）。
- **return_table**：年度 × R1..R5 表 → return_curve（仅供投资用，与收益流无关）。
- **initial_asset**：初始资产（现金/债券/股票/房产）→ 存量建档 `initial_asset`；现金进银行、股票债券一组、房产一组。
- **income_*（租/经营性房/祖产债券/开店）**：收益文件模块化 → 按属地模块拆 → `income_stream`（逐年现金流、挂 entity、直接取文件金额入账，不重算）。归属名 ↔ `entity.name` 失配标需人工。
- **salary**：养父/养母薪资 → 取文件税后值 → `income_stream(salary)` 挂其账户；跨币种按文件币种进各自本国货币池。
- **household_expense**：家庭支出 → 取"年度总支出"行 → `ledger` 支出（挂 Henri Peeters 账户）；2002 起停设。
- **event_movie / event_stock（Phase 2 启用，§19.6）**：基准/事件 的电影/股票事件素材，Phase 1 跳过、Phase 2 由数据调整员导入**不关联账户**，UI 用户按同位手动关联；event_stock → `holding_event`(batch) + `ledger_entry`(买入/卖出/分红/现金并购)。
- **fx**：`1EUR=40.3399BEF` → exchange_rate(fx_from, fx_to, year)。
- **character**：`- 字段：值` → entity + relationship（识别 关系 字段）。
- **timeline**：decade 表 → timeline_event；**merge_overlay**：将 user_data_overlay 改动应用到对应条目（`overlay=True`）。

### 6.4 覆盖层编辑（overlay/）
UI 编辑编年史 → 写入 `overlay_dir/编年史.md`（用户数据 md）。每次 merge：
1. 读覆盖层 md → `user_data_overlay` 解析。
2. 与 `timeline` 源条目按 `key`（年份+标题）定位。
3. 合并 → 写入 `timeline_event`（`overlay=True` 行），源条目保留（`overlay=False`）。
4. 可「重置回源」（删除覆盖层对应项）或「以源为最新」。

### 6.5 Phase 1 摄入：数据流因果链与收益挂账（用户定案）

**摄入是一条因果链，非并列表**：`初始资产 → 收益计算 → 进入银行`，同一对象只有一份存量档案，不重叠。

```
人物/ → entity（先导！entity_id 是收益挂账的锚）
初始资产(*.md) → 存量建档（现金→银行；股票债券一组；房产一组）
收益文件(4类: 租/经营性房/祖产债券/开店) + 薪资 → 逐年现金流 → 挂对应 entity → 进账户活期
支出文件(家庭支出) → 挂 Henri Peeters 账户 支出
```

- **ingest 顺序锁死**：`人物/` 先入 `entity` → 初始资产 → 收益文件(@entity_id) → 银行。收益模块挂 `entity_id` 依赖人物先导入。
- **归属主体来源 = 人物档案 `entity`**；收益模块按**属地/地域颗粒**拆分，各挂各主体；归属名 ↔ `entity.name` **失配进 `ingest_report` 标需人工**（不静默跳过，复用 `date_rule` 那套"补一条沉淀复用"）。
- **收益文件 = 模块化**：一个文件含多个属地子模块（如经营性房产收益=比利时/丹麦/荷兰…每模块一个补属主体）。模块即挂账单位。
- **股票债券归属粒度**：按**地域颗粒**打包挂一个人/公司（如"丹麦的股票债券"一个包挂某主体）；**包内收益仍分开计算**（每只股/每支债各自算票息），属同域同主体。
- **房产**：房产A/B/C 全算收益（经营性房产收益 + 惠民租房）；家庭主古堡也列收益。
- **薪资/收益文件已写死金额/税后/币种**：系统**直接取文件金额入账，不重算税率/CPI/人口分段**（文件即权威，系统只搬运+校验，保"数据算得平"）。
- **起始现金进余额**：初始资产里的**现金 = 直接进对应账户银行作为初始余额**，是后续"钱→收益/投资"的本金种子。
- **薪资/收益各归各主体账户**：养父薪资 → 养父账户、养母薪资 → 养母账户；收益各进各主体账户；**家庭支出统一记 Henri Peeters 账户**（故事设定，非分摊）。
- **支出**：家庭支出取"年度总支出"行入账（不重算人口分段/CPI）；**2002 起家庭支出设定停止**（无此文件后续）。
- **投资手动推进**：钱→收益测算表投资（R1–R5）→ 年末赎回分红进银行 → **次年需用户手动重投（非自动续投）**，可改地区/R/金额或转投股票/电影（Phase2）。

### 6.6 币种生命周期与池关闭（Phase 1/2 通用）

- **账户两级模型**：人物/公司 = 大账号(account) × 其下币种资金池(account.currency)。两级都可整体关闭。
- **BEF/LUF/NLG：2002-01-01 关闭** → 池进入只读终态（不可再存/投/换汇），历史流水可回溯展示；`EUR` 池开一条承接分录（从 BEF/LUF/NLG 划转 XXXX，带原币、金额、折算汇率），之后新流水在 EUR 池。收益文件已内置 EUR 金额。
- 含币种生命周期的转换，系统与文件统一（文件已写死 EUR，系统不另折）。

---

## 7. 汇率折算与杠杆口径（core）

#### 7.1 汇率折算（currency.py）
- 以 `exchange_rate` 表为准；逐年取当年最优可用汇率。
- 链式折算：`BEF→EUR→USD` 用历年汇率连乘；校验 `A→B→C` 回 `A→C` 闭合（§10）。
- 集团/账户最终归并为 `family:total` 时换算到一个基准币（展示层 **USD**）。
- **账务本币 / 展示 USD 分离**：记账始终用本币（收益以本币进本国货币池，不折算 USD）；只有**展示层家族总资产/wealth** 才把各币种经 `exchange_rate(year, ×→USD)` 折算成 USD **现值显示**，不改账、不进本币池。
- **同比币种分组、异币种分开核算**（源自源数据规则）。

#### 7.2 R1-R5 + 杠杆（leverage.py）
- 基准收益：`return_curve(country, risk_lvl, year).rate`。
- 杠杆后收益：`rate × leverage`（如 2 倍）。固定阈值按 `since_year` 生效（1.5→2 倍分界）。
- 逐账户逐年滚动：`balance_y = balance_{y-1} × (1 + rate_calc) + 净流入`。
- 提供 `recompute_one(account, from_year)` 供增量重算复用。

---

## 8. 逐年快照（snapshot.py）

- 预计算：`snapshot(as_of_date, scope, value)`，覆盖 account×币种、entity、family:total；`as_of_year` 由其派生（冗余、便于按年聚合）。
- 口径：`截至所选日历日 状态 = 固定值 + 累计到该日期的年标记变更`（§PRD 6.3）。
- 日历游标：前端按所选日期（年-月-日）读 `snapshot` 一次，无需实时计算；源数据缺粒度按 §6.2 默认规则补全到日。
- **全局日历游标（E）**：游标为全 App 级常驻（顶部固定，日历控件可精确到年-月-日），非仅 Dashboard；所有 人物/公司/财务/曲线 屏统一按 `as_of_date` 读取各自快照，拖动/选择一次全屏联动，观看「截至所选日期节点」的状态。
- 日期解析统一走 §6.2 的默认规则(F)+`date_rule` 表。
- 增量重算后只重建受影响年份起的快照（§9）。

---

## 9. 增量重算（recompute.py）

### 9.1 受影响起点确立
- **新增文件**：起点 = 文件内容中最早的可能影响年（如泰坦尼克首笔影响 1995 支出 → 1995）。
- **更新时间线/事件**：起点 = 该记录年份。
- **固定值变更**：起点 = 1（最早，因影响全局）——但固定值变更极少（仅覆盖层/回退触发）。

### 9.2 增量重算流程
```
1 确立 start_year（受影响起点）
2 事务内：
   a. 受影响范围 = 从 start_year 起的所有 account 余额、相关 entity/family 快照
   b. 对每个受影响 account：recompute_one(account, start_year)
   c. 重建 snapshot(start_year..max_year)
   d. 重跑 health 校验(受影响范围)
3 写 recompute_job(status=done, start_year) → 建 notification(recompute-done)
4 返回 job_id：前端据此弹「全局重算完成」非阻断提示，可查看影响范围
```
- **不全量**：不重建 start_year 之前的快照（除非固定值变更）。

### 9.3 重算提示（前端）
- `notification(kind='recompute-done')` 轮询/SSE → UI 用户看到横幅：`已在全局重算财富与派生数据（自 {year} 起）`+「查看影响」。
- 非阻断，可忽略；「查看影响」→ 展示受影响 account/entity/快照清单。

---

## 10. 健康校验（health.py）

| ID | 规则 | 检测 | 输出 |
|----|------|------|------|
| H1 时间线对齐 | timeline_event 年份 vs 台账/收益表相关年份 | 跨文件 JOIN 比对 | 问题(文件/行/规则) |
| H2 金额一致 | 同一标的在不同文件（台账 vs 事件 vs 收益表）金额一致 | 按 company/ticker 聚合 | 偏差清单 |
| H3 汇率链自洽 | A→B→C 折算回 A→C 闭合 | 连乘 vs 直接 | 偏差% |
| H4 复利/杠杆自洽 | balance 连续（源值 vs 重算值） | 逐账户 rolling | 断点 |
| H5 断链 | 关系/引用指向不存在 entity | 外键/名解析 | 孤儿引用 |
- 结果供两角色查看；可作为导入后「数据状态是否正确」的核对依据。

---

## 11. 文件变更流与回退

### 11.1 新增文件（数据调整员）
1. 放入 `input_dir` → 运行 `python -m app.ingest.main`。
2. detect → parse → normalize → 事务导入 → 增量重算 → notification。
3. `source_file_version` 记录 `is_current` 的新版本。

### 11.2 更新已有文件（diff + 决策）
1. 检测到 `input_dir/某文件` 与 `source_file_version.is_current` 内容不一致。
2. 生成 diff：`旧(current) vs 新(背书文件)` → 写入 `notification(kind='file-updated', payload=diff)`。
3. 前端展示 diff。
4. UI 用户决策 API（强 RESTFul）：
   - **采纳新版本**：`POST /api/v1/source-files/{id}/versions`（body = 新内容）→ 201 + 重新解析导入 → 增量重算 → 更新 `is_current`。
   - **回退到指定版本**：`POST /api/v1/source-files/{id}/versions/{vid}/restore` → **Postgres 保持上一版** 且 **把磁盘文件覆盖复原为上一版内容**（从 `source_file_version` 读旧内容写回 `input_dir` 该文件）。

### 11.3 回退写盘安全
- 仅对 `input_dir` 内文件写；绝不触碰 `source_dir`（Novel 设计源库）。
- 写回前校验：目标文件仍在 `input_dir`，且仍为"待回退"版本 → 原子替换。无鉴权，靠路径约束。

### 11.4 导入前冲突检测（hard-block）

**原则**：数据一致性在**文件导入时**由代码强制，**不在导入后**靠 LLM 或全量校验补救。新文件解析出的归一化记录在写库**之前**，与 DB 中相关既有记录做增量语义比对。硬矛盾 → **该文件不入库**（DB 保持干净，快照永远自洽）。

**触发**：新增/更新文件导入（§11.1/§11.2）第 2 步解析之后、事务导入之前。

**冲突规则**（对照 §10 的 H1–H5，改为"新 vs 旧"增量比对）：

| 冲突 | 判定 | 严重度 |
|------|------|--------|
| **金额冲突**（H2） | 新文件标的价值 vs DB 同一标的（company/ticker/账户）既有值 | 挡 |
| **余额断链**（H4） | 新流水行前余额 ≠ DB 现有末余额 + 收支 | 挡 |
| **时间线对齐**（H1） | 新事件年份 vs 相关台账/收益表既有年份 | 标 |
| **汇率/复利自洽**（H3/H4） | 新汇率记录使 A→B→C ≠ A→C 闭合失败 | 挡 |
| **断链/引用**（H5） | 新记录引用不存在的 entity | 标 |

- **挡** = hard block，file 不入库；**标** = soft warning，入库但高亮。
- 命中冲突一律写进 `ingest_report`，**精确告知冲突点**：`文件 / 行 / 规则 / 新旧值对照`，供数据调整员定位。

**处置链（不新增权限面）**：
```
新文件 → 解析 → 冲突检测
  ├─ 无冲突     → 导入 → 增量重算 → notification(UI 用户)
  └─ 有冲突     → 不入库，进 ingest_report（file/line/规则/新旧值）
                  → 由【数据调整员】在文件侧解决（修文件重导 / 确认覆盖）
                  → 版本级采纳/回退仍由【UI 用户】判定（§11.2 diff 决策）
```
- 语义冲突由数据调整员在文件侧解决，不入索引；版本采纳/回退由 UI 用户在 diff 屏判定——职责与 PRD §4 权限映射一致。
- **与 health.py 分工**：`conflict.py` 负责导入瞬间的增量拦截；`health.py` 保留为导入后全库 H1–H5 汇总视图（§10），两者不重复承担同一职责。

---

## 12. 日历年编年史覆盖层编辑（P2，§6.4/overlay）

- 前端进入「编年史」屏 → 增改删时间线条目。
- 变更写入 `overlay_dir/编年史.md`（用户数据 md 覆盖层），**不写源 md**。
- 支持：保存、看差异（覆盖层 vs 源）、重置回源、以源为最新。
- merge 后进 `timeline_event(overlay=True)`，触发增量重算（起点=条目年份）。

---

## 13. 公司与用工成本外部系统（P1）

公司与用工成本依赖**一个外部系统**，通过 **2 个独立 API** 对接（接口细节开发时定）。可插拔适配器：`ingest/importers/{provider}.py`。

### 13.1 API① 公司基础信息
- 外部系统持有公司权威信息 → 拉取 → 公司图谱。
- **只增不减**：导入时按 `(entity_type='company', name)` upsert → 更新 `status` 等信息，**禁止 DELETE**（应用层规则）。
- 例外通道：仅管理员手动维护窗口可触发 `python -m app.ingest.main --admin-clean`（开发时定），普通流程永不调用。
- 唯一删除入口：`DELETE /api/v1/entities/{id}` 仅在 `--admin-clean` 模式下生效（服务端据环境/模式拒绝）；非该模式的普通流程对该端点一律 409。
- 每个公司带状态字段 `status`（取值开发时定）；source 标识 = `'external-api'`。
- 触发：UI 用户在公司图谱屏点「获取/导入」按钮。

### 13.2 API② 用工成本计算
- 输入：本地**逐年「用工成本 + 税率」基准**（`基准/公司/用工成本/*.md`、`基准/公司/用工成本/税率/*.md`）。
- 请求外部 → 返回**当年各公司用工成本支出**。
- 导入 → 落到各公司下的**支出** `finance_entry(entity_kind='company')` → 增量重算。

### 13.3 统一适配器接口
```python
class Importer(Protocol):
    def fetch(self, payload) -> list[dict]: ...   # 外部 API 原始数据
    def to_normalized(self, raw) -> Normalized: ...
```
- 配置 provider + 凭据（本地）；结果出现在「导入状态」屏；失败进入需人工处理。
- **凭据存放**：外部 API 凭据放本地 `secrets.local.yaml`（**不入 git、不入 DB**）；adapter 只通过配置键读取，不在日志或 notification 中泄露。

---

## 14. API 端点清单（严格 RESTFul，FastAPI）

### 14.1 REST 约定
- **资源名词**：URL 用**复数名词**（`entities`、`timeline-events`、`finance-entries`）；动作以 HTTP 动词表达。
- **HTTP 方法**：`GET` 读取 / `POST` 创建 / `PUT` 全量替换 / `PATCH` 局部更新 / `DELETE` 删除；幂等性按 HTTP 语义。
- **状态码**：`200 OK` / `201 Created` (+ `Location`) / `204 No Content` / `400` 参数错 / `404` 资源不存在 / `409` 冲突（如幂等键冲突）/ `422` 校验失败 / `500` 服务器错。
- **集合**：支持 `?filter=`、`?sort=`、`?page=`、`?page_size=`、`?as_of=YYYY-MM-DD` 查询参数；分页默认 `page_size=50`。
- **子资源**：父子关系用嵌套 URL（`/api/v1/source-files/{id}/versions`），不暴露动词。
- **异步动作**（import / recompute / export）建模为 **job 资源**：发起即创建 job，子操作变成对该 job 的状态查询（避免 RPC 风格 `/run`、`/accept`）。
- **视图资源**（多资源聚合）以 `/api/v1/overview`、`/api/v1/graph/{kind}`、`/api/v1/wealth`、`/api/v1/returns`、`/api/v1/finance` 提供，只读 GET。
- **URL 版本段**：所有内部 API 一律以 **`/api/v1/`** 为前缀；后续不兼容演进发布 v2 时**并行运行 v1**（v1 进入维护期，仅修 bug、不加新端点），客户端可显式指定版本。
- **写端点授权**：除 `timeline-events`（编年史，经覆盖层）与 `source-files/{id}/versions*`（diff 决策）外，其余**写端点**（创建/全量替换/局部更新/删除 `entities`、新增 `ledger-entries`、新增 `finance-entries` 等）**不面向普通 UI 用户**，仅供 importer / 数据调整员（受限通道，对齐 PRD §1.4 铁律）。普通 UI 对这类写端点的调用应由 serve 层拒绝（409/403）。

### 14.2 资源端点

#### 实体 / 关系 / 图谱
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/entities` | 列出实体（过滤 `?type=person\|company\|asset\|family`、`?status=…`） |
| GET | `/api/v1/entities/{id}` | 单实体详情 |
| POST | `/api/v1/entities` | 创建实体（受限通道） |
| PUT | `/api/v1/entities/{id}` | 全量替换（受限通道） |
| PATCH | `/api/v1/entities/{id}` | 局部更新（含 `status`；受限通道） |
| DELETE | `/api/v1/entities/{id}` | 仅 `--admin-clean` 通道，普通流程 409 |
| GET | `/api/v1/entities/{id}/relationships` | 实体的关系列表 |
| POST | `/api/v1/entities/{id}/relationships` | 建立新关系 |
| DELETE | `/api/v1/relationships/{id}` | 删除关系 |
| GET | `/api/v1/graph/persons` | 人物图谱视图（只读） |
| GET | `/api/v1/graph/companies` | 公司图谱视图（只读） |

#### 账户 / 流水 / 持仓 / 收益 / 汇率
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/accounts` | 列表（过滤 entity/currency） |
| GET | `/api/v1/accounts/{id}` | 详情 |
| GET | `/api/v1/accounts/{id}/ledger-entries` | 银行台账流水（日期范围过滤） |
| POST | `/api/v1/ledger-entries` | 新增流水（手工纠错用；受限通道） |
| GET | `/api/v1/ledger-entries/{id}` | 详情 |
| GET | `/api/v1/holding-events` | 股票持仓事件列表 |
| GET | `/api/v1/return-curves` | 收益曲线列表（国家 × 风险级 × 年份） |
| GET | `/api/v1/exchange-rates` | 汇率列表 |

#### 时间线 / 编年史
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/timeline-events` | 编年史列表（`?as_of=…`、`?decade=…`） |
| GET | `/api/v1/timeline-events/{id}` | 详情 |
| POST | `/api/v1/timeline-events` | 创建条目（写入覆盖层） |
| PUT | `/api/v1/timeline-events/{id}` | 全量替换 |
| PATCH | `/api/v1/timeline-events/{id}` | 局部更新 |
| DELETE | `/api/v1/timeline-events/{id}` | 删除覆盖层条目（源仍保留） |
| POST | `/api/v1/timeline-events/{id}/overlay/restore` | 重置回源（删除覆盖层，恢复源条目） |

#### 财务 / 健康 / 快照 / 通知 / 财富
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/finance-entries` | 财务收支列表（过滤 `entity`/`person\|company`/`kind`/`year`） |
| POST | `/api/v1/finance-entries` | 新增（外部导入/纠错；受限通道） |
| GET | `/api/v1/health` | 健康校验汇总（singleton，只读） |
| GET | `/api/v1/snapshots` | 快照列表（`?as_of=YYYY-MM-DD&scope=…`） |
| GET | `/api/v1/snapshots/{date}` | 某日期快照 |
| GET | `/api/v1/notifications` | 非阻断提示列表 |
| PATCH | `/api/v1/notifications/{id}` | 标记已读（`{"read_at": "now"}`） |
| DELETE | `/api/v1/notifications/{id}` | 忽略/删除 |
| GET | `/api/v1/overview` | Dashboard 视图（只读） |
| GET | `/api/v1/wealth` | 财富曲线视图（只读） |
| GET | `/api/v1/returns` | 各国收益曲线视图（只读） |

#### 源文件 / 版本 / 回退
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/source-files` | 输入文件夹文件清单 |
| GET | `/api/v1/source-files/{id}` | 单文件元信息 |
| GET | `/api/v1/source-files/{id}/versions` | 版本列表（含当前 `is_current`） |
| GET | `/api/v1/source-files/{id}/versions/{vid}` | 单版本内容 |
| POST | `/api/v1/source-files/{id}/versions` | **采纳新版本**（body = 新内容；201 + 触发重算） |
| POST | `/api/v1/source-files/{id}/versions/{vid}/restore` | **回退到指定版本**（DB+磁盘复原） |

#### 导入 / 重算（异步 Job）
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/import-jobs` | 触发导入任务（body：`{provider:'company-info'\|'labor-cost', payload:{...}}`） |
| GET | `/api/v1/import-jobs` | 任务列表（过滤 provider/status） |
| GET | `/api/v1/import-jobs/{id}` | 任务详情（含阶段进度） |
| DELETE | `/api/v1/import-jobs/{id}` | 取消待执行任务 |
| POST | `/api/v1/recompute-jobs` | 触发重算（body：`{start_year, reason, files:[...]}`） |
| GET | `/api/v1/recompute-jobs/{id}` | 任务详情 |

#### 日期规则 / 导出 / 搜索
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/date-rules` | 日期解析规则列表 |
| POST | `/api/v1/date-rules` | 补充一条规则 |
| PUT | `/api/v1/date-rules/{id}` | 全量替换 |
| DELETE | `/api/v1/date-rules/{id}` | 删除规则 |
| POST | `/api/v1/exports` | 创建导出任务（body：`{format:'markdown'\|'csv'\|'pdf', scope?}`）→ 201 + 下载 URL |
| GET | `/api/v1/exports/{id}` | 获取导出产物 |
| GET | `/api/v1/search` | 统一搜索（LLM+agentic RAG，见 §18；Phase 1/P1-5 实施） |

---

## 15. 导出（export/）

- **markdown**：按当前生效数据（源+覆盖层合并）渲染结构化 md，可作新素材档案。
- **CSV**：财务、收益、持仓等表格。
- **PDF**：报告（图表内嵌）。
- 原则：仅导出，不写任何源/输入文件。

---

## 16. 三环境 / 迁移（Phase 2）

- 三库独立；Alembic 迁移共用。
- Phase 2 需求：跨环境数据同步/迁移（如把 test 修正同步回 prod），按使用情况再设计（目前仅说明意图，不实现）。

---

## 17. 测试策略（test 环境）

- dev/test 共用真实数据；test 做**抽样验证**（如某文件 30 条取 2 条核对余额连续性）。
- 单元：各 parser（固定/年标记判定、千分位、多币种）。
- 集成：新增文件导入→重算→校验；更新文件 diff→accept/revert（断言 DB 与磁盘结果）。
- 快照重建：指定年份 as-of 与手算一致。

---

## 18. 统一搜索（LLM + agentic RAG）

> 状态：**已纳入实施**（Phase 1 / P1-5）。本地模型实测可用（见 §18.5）。

### 18.1 用户视角与边界
- **UI 用户无"文件"概念**：搜索输入问题 → 返回**最终答案主体**，由命中的**条目/句子**装配，不按文件分组呈现。
- **推理过程（CoT）不展示**：UI 只给最终答案，不输出推理步骤、不展示来源标注。
- **LLM 永不参与数值计算/一致性判断**：数值层一律走确定性 SQL / 快照 / 重算（对齐 PRD 铁律与 §11.4）。

### 18.2 索引与召回
- **粒度 = 条目/行级**：按 person / account / event / 曲线段等结构化单元分段**索引 embedding**，不用纯文本句子切表（避免把表格行切坏），也不按文件存储。
- 索引落在 **Postgres**（复用三环境独立库，`pgvector` 或 JSONB）。
- **纯条目级向量召回**为主路径：问题 → embedding → 命中相关条目标段 → 候选装配。不引入"LLM 先判文件再定向读"的兜底层（文件总数小，命中率足够；若后续召回质劣再叠加）。

### 18.3 装配
- 召回 top-k 条目标段 → LLM 将候选**条目标段 + 需要时并入的确定性数据**（SQL 快照/重算结果）组织为最终答案。
- 来源标注 = 命中的条目一句，**仅供内部审计/定位**，不上 UI。

### 18.4 与 search 端点
- `GET /api/v1/search` ：入参问题（+可选 `as_of` 日历游标）；出参 `{ answer, hits[] }`，`hits[]` 为命中的条目标段（内部用，前端不展示推理明细）。

### 18.5 本地模型选型（omlx-server，无鉴权）
- **端点**：`http://127.0.0.1:8000`（`/v1/chat/completions`、`/v1/embeddings`），无鉴权。
- **装配 LLM**：`Qwen3.8-27B-MLX-4bit`（实测可用：答案正确、防幻觉——无数据答「未提供」、中文）。
- **Embedding**：`Qwen3-Embedding-8B-4bit-DWQ`（实测可用：中文向量正常）。
- **配置项**（`config.py` 或独立 `LLM` 配置段）：`LLM_URL`、`EMBED_URL`、`LLM_MODEL`、`EMBED_MODEL`、`LLM_MODEL_CONTEXT`（1M 参考）、`EMBED_DIM`（由模型输出确定后固化）。
- **不采纳候选（实测）**：`Qwythos-9B-Claude-Mythos-5-1M-mxfp4-mlx`（强内嵌 CoT 吞光答案、不服从输出指令）；`glm-4-9b-chat-1m-8bit`（omlx 归为非 chat 模型）；`DeepSeek-V4-Flash-MTP-bf16`（加载失败，参数不匹配）。

### 18.6 serve 层输出约束（模型不可靠的补偿）
- `Qwen3.8-27B` 在极简指令下仍可能残留「复述问题 / 列步骤」包装（但不影响答案正确性）。serve 层须做**后处理**：剥离 CoT/复述头、去首尾多余步骤编号，返回`answer` 仅最终答案。
- **前端硬性剪裁**：只渲染 `answer` 字段，任何命中 `1.` `2.` `分析` `步骤` 之类的推理文本不回显（对齐"不展示推理过程"决策）。
- **数值铁律**：LLM 输出绝不含确定性未核实的数值；凡涉金额/汇率/复利，须由 SQL/快照给出并回填，LLM 不生成数字（§11.4 同源）。

---

## 19. UI 财务派生操作与资产模型（投资 / 划拨换汇 / 事件关联 / 股票）

> 状态：已定案（PRD §6.7–6.9 / P1-6）。UI 用户可改数据固化为四类（§6.8）；投资(P1) 落 `investment`+`investment_alloc`，事件/股票(Phase2) 落 `holding_event`(batch)+`ledger_entry`，划拨/换汇落 `ledger_entry`；均**派生数据**，编年史 overlay 可见，**绝不回写源 md**。

### 19.1 投资功能（P1）

### 19.1 触发与输入
- 操作：选 年份 → 地区(欧洲/英国/美国/香港/中国，对应 `return_curve.country`) → 多选人物/公司 → 各主体按币种输入金额（或「全部」）→ R1–R5 → 提交。
- **一年一次**：`investment` 上 `UNIQUE(year, region)`；已投年份 UI 置灰，解锁=整笔抹除重输，提交原子覆盖该年并触发向后重算。
- `investment_alloc.amount ≤ 该主体该币种 as-of 余额`；「全部」（`is_all=true`）= 该主体所有币种账户一次全投各币种 as-of。

### 19.2 计息 / 赎回 / 池子
- **计息**：发生日 `start_date` 起，`当年该地区 R ÷365 × 持仓天数`，`天数 = 该年-12-30 − start_date`；`start_date = 12-30` 时天数 0、收益 0。R 可为负，公式通吃正负（亏损即 count）。
- **赎回**：每年 **12-30** 本金+收益从专款池划回银行账户、专款池清空；次年不自动续投（可换地区/R/金额，或趴活期）。
- **活期**：未划拨资金留银行账户，默认 2% 年化按日折；不设独立余额。
- **专款池（年度投资资金，分币种）**：投资本金=从银行 `ledger` **划出**入池（银行-）；赎回/补活期=划回（银行+）；**专款池本身不直接记支出**。支出只在银行账户发生：活期不足→先从专款池划回活期→再从银行支出。
- **走账模型**：银行 `ledger_entry` 承担全部收支（划出/划回/支出），保 `后一=前一+入−出`；专款池是银行的"在途仓"，非跨年持仓（年末清空）。对应 `finance_entry`：划入=kind'pool'（或投资本金）、年末收益=kind'investment_income'。

### 19.3 校验（服务层，UI 派生通道的瘦版 hard-block）
- 金额 ≤ 该币种 as-of 余额（422）
- 地区起始年下限（欧洲1947/英1983/美1989/港1999/中2002）— UI 选项下限 + serve 校验（422）
- 每「年份+地区」一年一次幂等（409）
- 覆盖连锁：若前序改动致后年「全部」超新 as-of → **整体拒绝该改动**（保守挡停，不自动压缩、不追手动重输）

### 19.4 重算与净值
- **重算**：向后最小传播；固定金额（`is_all=false`）冻结不追溯；仅「全部」（`is_all=true`）与 as-of 变更向后传播（因每年末重置，传播最长相邻年）。
- **净值**：财富曲线 = 银行 + 专款池合计；投资期间（发生日~12-30）总净值**不变**（资金在途不单独下沉），仅年末结算收益（含负收益）才令总净值变化。

### 19.5 划拨 / 换汇（P1，UI 类操作）
- **触发**：输入 **年份** → 选账户 → 源币种资金池 → 目标币种。同币种移动 = 划拨（净额 0，改 ledger 两笔同币一进一出）；跨币种 = 换汇（需 `exchange_rate` **该年份**汇率）。
- **gate**：换汇命中该年汇率才可换；缺则拦，数据调整员按需导入该年汇率后再操作；不做全时段穷举。
- **校验**：**转出年起向后全链 as-of 不得为负**，任一年拐负 → 整体拒绝转出（同投资选 i 的整体拒绝，复用同一套向后重算传播校验）。划拨目标可为另一实体账户（人物 A→公司 A）。
- **记年**：锁输入年份；落 `ledger_entry` 后传重算；同步编年史 overlay。

### 19.6 事件·股票与资产模型（Phase 2）
- **导入链**：`基准/事件/股票/**` Phase 2 重新纳入 detect → 数据调整员导入**不关联账户** → UI 用户按**同币种**手动关联到某账户(人物/公司)；现金流经事件生成 `ledger_entry` 进账户。
- **总资产**：`银行账户余额(现金) + 投资专款池 + 股票持仓市值`；现金与市值不重叠（买入=ledger 支出「购入股票」移除现金）。
- **持仓**：`holding_event` 需 **batch 维度**（每批买入=一个 batch，各自成本）。分红=每股派息×加权平均持仓→现金收入进账户。被动抬升占比（回购缩股本）持股不变、无现金动作。
- **成本 = FIFO**：卖出/减持从最早批次扣成本，`盈亏=卖出现金−批次成本`，进账户；成本仅供卖出结算，不参与总资产。
- **并购三形态**：
  1. 纯换股/分拆（HPQ→HP+HPE；2DXC→1PRSP）→ 只改持仓、成本按比分摊、现金不动。
  2. 换股+现金（MVL→DIS：1股=30现金+0.7452DIS）→ 换股部分成本随链(MVL成本→DIS批次)；现金部分进余额单独核算。
  3. 纯现金并购（Perspecta→Veritas）→ 持仓归 0、现金进余额、**不记盈亏**。
- HP_CSC 链（HPQ→DXC→PRSP→HPQ 逐年减持）经规则核验闭合；数值逐行对齐待 Phase 2 实际导入后由 §11.4 + H2 验证。

---

## 20. 开发功能清单（按优先级，供跟踪）

> 状态图例：⬜ 待做 · 🟨 开发中 · ✅ 已完成 · ⛔ 组内置灰（暂不做）。勾选随实现推进更新。
> **编号**：`F-P0-xx`（Phase1·P0）、`F-P1-xx`（Phase1·P1）、`F-P2-xx`（Phase2），供文档/需求/任务引用。

### Phase 1 —— P0（核心可用）

| 编号 | 模块 | 功能 | 关键章节 | 状态 |
|---|---|---|---|---|
| F-P0-01 | **工程骨架** | config 三环境 / db / alembic 迁移 / ingest CLI | §3–§4 | ✅ |
| F-P0-02 | **Ingest** | detect 类别识别 + 解析器(bank/股票表/汇率/人物/时间线) + normalize | §6 | ✅ |
| F-P0-03 | **Ingest** | 导入前冲突检测 hard-block（conflict.py，§11.4 + 金额/余额等） | §11.4 | ✅ |
| F-P0-04 | **Phase1 摄入** | 初始资产建档(initial_asset) + 现金进余额 | §6.5 | ✅ |
| F-P0-05 | **Phase1 摄入** | 收益文件模块化挂账(income_stream)：租/经营性房/祖产债券/开店（薪资在 F-P0-06） | §6.5/§6.3 | ✅ |
| F-P0-06 | **Phase1 摄入** | 家庭支出(挂 Henri)/薪资各归各账户；2002 BEF/LUF/NLG 关池转 EUR | §6.5/§6.6 | ✅ |
| F-P0-07 | **DDL** | entity/account(status/closed_on)/ledger_entry/income_stream/initial_asset/snapshot 等 | §5 | ✅ |
| F-P0-08 | **快照** | 逐年 as-of 快照预计算（snapshot.py） | §8 | ✅ |
| F-P0-09 | **财富曲线** | 账户/币种/公司/全家族合计 + USD 展示折算（账务本币/展示USD） | §7/§8 | ✅ |
| F-P0-10 | **日历游标** | 全局日历 as-of 拖拽，全 App 联动（服务函数就绪，API 端点 F-P0-13） | §8 | ✅ |
| F-P0-11 | **健康校验** | H1–H5 汇总 + 问题清单（health.py） | §10 | ✅ |
| F-P0-12 | **增量重算** | 受影响起点向后传播（recompute.py）+ 提示 | §9 | ✅ |
| F-P0-13 | **API** | 基础路由（entities/accounts/ledger/returns/fx/snapshots/overview/wealth） | §14 | ⬜ |
| F-P0-14 | **前端骨架** | React+Vite 10 屏骨架 + 全局日历 + 财富曲线 | §1 | ⬜ |

### Phase 1 —— P1（完整产品）

| 编号 | 模块 | 功能 | 关键章节 | 状态 |
|---|---|---|---|---|
| F-P1-01 | **投资** | investment + alloc 派生数据、一年一投、R 级计息、年末赎回回银行、手动续投 | §19.1–19.4 | ⬜ |
| F-P1-02 | **投资** | 专款池（分币种）走账 + 服务层校验（as-of/R起始年/年度幂等/覆盖连锁拒绝） | §19.3 | ⬜ |
| F-P1-03 | **划拨/换汇** | 划拨(同币)/换汇(按年汇率) + 转出向后全链不破负拒绝 + 编年史同步 | §19.5 | ⬜ |
| F-P1-04 | **人物图谱** | 人—人/人—公司关系可视化（ECharts graph） | §1/§14 | ⬜ |
| F-P1-05 | **公司图谱** | 公司—公司关系 + 外部 API①② 导入（只增不减 + status） | §13 | ⬜ |
| F-P1-06 | **各国收益曲线** | return_curve（R1–R5）对比渲染 + 地区起始年下限 | §14 | ⬜ |
| F-P1-07 | **财务收支** | finance_entry 各类收入/支出，实体必填、以实体为中心浏览 | §5 | ⬜ |
| F-P1-08 | **统一搜索** | LLM+embedding RAG（omlx 本地）条目检索装配 + serve 后处理 | §18 | ⬜ |
| F-P1-09 | 四类 UI 改数据操作 | 统一模板：年份×池 + 后传重算 + 失败整体拒绝 + overlay 同步（§6.8 全清单） | §6.8/§19 | ⬜ |

### Phase 2 —— 事件 / 增强

| 编号 | 模块 | 功能 | 关键章节 | 状态 |
|---|---|---|---|---|
| F-P2-01 | **事件·电影** | 电影事件导入 + 不关联 + 同币种 UI 手动关联（现金流入账户） | §19.6/§6.9 | ⬜ |
| F-P2-02 | **事件·股票** | holding_event(batch) + FIFO 成本 + 分红/卖出结算 + 被动抬升占比 | §19.6/§6.9 | ⬜ |
| F-P2-03 | **事件·股票** | 分拆 / 并购三形态（换股+现金/纯现金/纯换股）成本随链 | §19.6 | ⬜ |
| F-P2-04 | **事件·股票** | HP_CSC 重组链数值导入 → 依 §11.4 + H2 逐行验证 | §19.6 | ⬜ |
| F-P2-05 | **时间线/编年史 UI 编辑** | overlay 增改删、差异/重置回源/以源为最新 | §12/§6.4 | ⬜ |
| F-P2-06 | **文件 diff 回退** | 版本 diff → UI 决策「更新」/「回退」（DB+磁盘复原） | §11 | ⬜ |
| F-P2-07 | **导出** | markdown/CSV/报告 PDF（只导出不回写源） | §15 | ⬜ |
| F-P2-08 | **统一搜索增强** | 搜索数据质量达标后再评估是否需要 LLM 判文件兜底 | §18.5 | ⬜ |

> 推进原则：先 P0 工程骨架打通 ingest→快照→曲线→健康；再 P1 各交互操作；Phase 2 事件/增强。跨 P0/P1 共用 §6.8 四类操作模板，避免重复实现。