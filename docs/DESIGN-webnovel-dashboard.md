# DESIGN — 网文创作数据 Dashboard

对应 [PRD-webnovel-dashboard.md](PRD-webnovel-dashboard.md) 的技术设计文档。目标：把 PRD 落成可实现的架构、数据模型(DDL)、解析器、增量重算算法、文件变更流与 API 契约。

> 版本：1.0（草案） · 三环境：dev / test / prod（三个独立本地 Postgres 库）
> UI 原型：[ui-mockup/index.html](ui-mockup/index.html)（七轮 #185 补录互引；实现以 frontend/src/App.jsx TABS=16 为准）

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
   │  ├─ main.py             # CLI: python -m app.ingest.main（含 reset 清库重建子命令）
   │  ├─ manifest.py         # prod 激活清单 import_files.yaml 门控（dev/test 不读，§11.1）
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

**prod 运维文件（位于 gitignored 的 `Design_Folder`，不入 git）**：
- `Design_Folder/import_files.yaml` —— **激活清单**（严格白名单）：枚举 Design_Folder 全部 `.md`（含 `基准/事件` 电影/股票及子目录、`基准/公司/用工成本/` 与 `税率/`、模版/设计文件等），仅 `active:true` 的源文件才会在 **prod** 被 ingest/events-movie/events-stock/labor-baseline 导入；dev/test 不读本清单、维持全量导入（§11.1/§16）。
- `Design_Folder/start_dashboard.sh` —— 启动脚本：`APP_ENV=prod` 起后端 FastAPI(8001) 与前端 Vite(5173)，仅启服务（不含重置/导入）。

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
- 初始化：`alembic upgrade head && python -m app.ingest.main ingest`，无 UI 向导。
- **prod 导入门控（Phase 2+ 运维）**：`Design_Folder/import_files.yaml` 为激活清单（严格白名单），prod 的 `ingest`/`events-movie`/`events-stock`/`labor-baseline` **只导入 `active:true`** 的源文件，未激活一律跳过（manifest.py `require_active_files`）；dev/test 不读清单、全量导入现状。新增源文件需先在清单登记并激活。
- **清库重建（不可逆）**：`reset --env prod`（`--yes` 跳过确认）——删 `novel_prod` **全部 public 表**（保留 pgvector 扩展、库本体、DSN）→ `alembic upgrade head` 重建空 schema。

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

> **实现超集（issue #33 回写）**：上表 DDL 为设计基线，实际 `app/model/core.py` / `derived.py`（经 alembic 落地）在之上新增列/约束/索引，alembic autogenerate 应以实现为准：
> - `ledger_entry` 新增 `kind` 列（`income/expense/investment/investment_income/pool`，CHECK `ck_ledger_kind`）+ 索引 `ix_ledger_acct_date(account_id, date)`。
> - `initial_asset` 加 CHECK `ck_initial_asset_type`（`cash/bond/stock/property`）。
> - `income_stream` 的 `stream_type` 收严为 `NOT NULL`，并加 CHECK `ck_income_stream_type`（`rent/property/security/shop/salary`）。
> - `holding_event` 加索引 `ix_holding_entity(entity_id, company)`。
> - `timeline_event` 加索引 `ix_timeline_year(event_year)`。
> - `finance_entry` 另含 CHECK `ck_finance_kind` 与 `ck_finance_entity_kind`（`person/company`）。
> - `entity.source` 含 server_default `'file'`（issue #132/#145：超出下文五项清单的第六项）。
> - **异步任务表（issue #138 · 方案A）**：新增 `import_job`（id/provider CHECK('company-info','labor-cost')/payload JSONB/status CHECK(pending,running,done,failed)/result JSONB/error/created_at/finished_at），承载 §14.2 import-jobs；recompute-jobs 复用上表 `recompute_job` 并启用完整 pending→running→done/failed 生命周期。

---

## 6. 解析器设计

统一 `parse(path)` → `NormalizedFile`（每个文件一批已归一化记录）。失败不阻塞其他文件，进入 `ingest_report`。

### 6.1 类别识别（detect.py）
按路径 `input_dir 下的相对路径` 匹配：
| 相对路径前缀 | 类别 |
|---|---|
| `经济/银行/` | bank |
| `经济/股票/` | stock_tx |
| `基准/收益表/全球五地…（史实版）.md` | return_table（R1–R5 五地逐年收益，仅供投资；issue #214 整合单文件，Markdown 表格型） |
| `基准/收益表/基本收入.md` | **basic_income**（五人初始资产逐年收益终值：股债 security / 惠民租房 rent / 经营性房产 property / 祖父开店 shop；issue #211） |
| ~~`基准/收益表/惠民租房.md`、`经营性房产收益.md`、`祖产股票债券收益.md`、`祖父开店.md`~~ | **已删除**（2026-09，issue #211 收尾：被 基本收入.md 完全取代后从 Design_Folder 移除；detect 的 SKIP_SUPERSEDED 规则保留为防误放回护栏） |
| ~~`基准/收益表/1947-2025 欧洲…测算表.md`、`1983-2025 英国…`、`1989-2025 美国…`、`1999-2025 香港…`、`2002-2025 中国…`~~ | **已删除**（2026-09，issue #214：5 张分地区 R1–R5 表整合为全球五地单文件，1050 个键值逐一核对零差异后移除；detect 的 SKIP_SUPERSEDED 规则保留为防误放回护栏） |
| `基准/初始资产/*.md` | initial_asset（存量/本金建档） |
| `基准/薪资/养父\|养母的薪资_CNY修正版.md` | salary（逐年薪资收入，取文件税后值；issue #220 起为 CNY 修正版，按人替换式落库） |
| ~~`基准/薪资/养父的薪资.md`、`养母的薪资.md`~~ | **已删除**（2026-09，issue #220：被 `_CNY修正版` 完全取代——中国段结算币种 USD→CNY、退休奖金改比利时 Assigned out 退职金口径（2 倍基薪/EUR/18% 优惠税率）、养父美国段奖金率修正为规则声明的 12%；detect 的 SKIP_SUPERSEDED 规则保留为防误放回护栏） |
| `基准/1974-2001家庭支出.md`（及 CPI 基准） | household_expense（家庭支出，挂 Henri 账户；issue #216 起为修正版——CPI 史实验证/KU Leuven 明细等说明扩充、28 年逐年数值不变，prod 同步激活） |
| `基准/公司/用工成本/**`（2 个汇总文件） | **SKIP_P1**（主扫描链不导入；由独立 CLI `labor-baseline` 落 labor 三表供 API②，§13.2。issue #218 整合为 `用工成本汇总_12地_CPI修正版.md`（12 地区工资+CPI）+ `各国雇主社保税率汇总（逐年展开版）.md`（11 节覆盖 12 office）；旧 10 国工资分文件 + `税率/` 12 分文件已删除并入） |
| ~~`基准/CPI工资.md`~~ | **已删除**（2026-09，issue #218：工资增幅/CPI 并入用工成本 12 地汇总；detect 的 SKIP_PARAM 规则保留为防误放回护栏） |
| `基准/汇率/` | fx |
| `人物/` | character |
| `时间线.md` | timeline |
- **阶段项**：`基准/事件/**`（电影/股票等事件素材）**Phase 1 跳过**；**Phase 2 重新启用**（对齐 PRD §2.3 按阶段导入范围与 §19.6）。Phase 1 detect 遇到 `基准/事件/电影|股票/` 归 `event_movie/event_stock`（解析器就绪，由 `events-movie/events-stock` CLI 显式导入，主扫描链 ⏭ 显式跳过）；其余散文件归 `SKIP_PHASE2_EVENT` 直接跳过（issue #144：不再落"解析器未实现"error）、不建 `holding_event`；Phase 2 恢复 `event_movie` / `event_stock` 解析器并由数据调整员导入后 UI 同位手动关联账户。
- **模版略不同**：解析器容忍表头/分隔符差异（`\|` 或 `\t`）、单位列缺失；识别失败 → 归类"需人工处理"，不入库。

### 6.2 通用文本工具（normalize.py）
- **数字**：去千分位逗号、`万`/`亿` 单位、`≈`、`≈X.XX` 四舍五入。
  > ⚠️ 实现注记（issue #145）：`normalize.parse_amount` 对 `万/亿` 仅做**剥离、不换算**数量级
  > （test-pinned 工具函数，调用方按所在文件的既有单位口径直取）。误用于需换算的场景会差
  > 万倍/亿倍——新增解析器时须先确认该列在源文件中的单位约定。
- **货币**：`(万)USD/BEF/LUF/NLG/DKK/SEK/HKD/EUR` 后缀识别；无显式则继承所在节币种。
- **日期（统一日历年，无"财年/时间尺"口径）**：`YYYY` / `YYYY-MM-DD`；全系统用**日历历法（1/1–12/31，结算 12-30）**；日历覆盖区间 **1947（最早）– 最晚年**。最晚年收敛于 `app/config.CALENDAR_MAX_YEAR = max(2026, 当前年+1)` 动态推导（issue #141：此前 2025/2026 字面量散落十余处，跨入上限年即静默停滚）；源数据若出现"某财年(6-30 截止)"，须**归一为日历年日期**，不留财年维。`1995-01-01`→as_of_year=1995。
  - **源数据缺失粒度 → 默认规则补全（F，日历与解析共用）**：仅提供年份 → 当年 `12-30`（注明「年初」→`01-01`）；年份+月份 → 该月**月底**（写明「月初」→该月 `1 日`）；「上旬/中旬/下旬」→`1 日/11 日/21 日`。全局日历可精确到年-月-日，但源数据只给到"年或年月"时按此规则补全到日。
  - **超规则处置**：源数据日期无法套用任何已知规则 → 报「需人工」并**提醒 UI 用户**；用户**补充一条 `date_rule`**（`POST /api/v1/date-rules`），后续解析复用该规则。
- **固定 vs 年标记**：行含年份列/日期列 → 年标记；否则 → 固定值（写入 entity.fields 或常量表）。

### 6.3 各解析器要点
- **bank**：按 `## 一、…BEF（祖父）` 分币种节；每节读表列 `日期|理由|收入|支出|余额|备注`；`account_id` 由 `entity × currency × bank` 唯一确定。**余额**：存入源值；连续性校验位置更正（四轮审计 #171）：导入期 conflict H4 仅查「新首笔前值 vs DB 末余额」衔接，全链连续由导入后 health.check_h4_balance_chain 兜底——非字面「于 normalize 校验」。币种识别（#162）：节标题「中文币种词+缩写」配对优先，防多币种标题误判。文件名含「模版/模板」→ SKIP_TEMPLATE 不导入（#168）。
- **stock_tx**：`### 基本信息` 列表 → 常量入 entity.fields；`### 年度明细` 表 → holding_event。拆股/换股链可由 `event_type` 关联出一张 `relationship`（acquired/split）。
- **return_table**：年度 × R1..R5 → return_curve（仅供投资用，与收益流无关）。两种格式：① 全球整合文件（issue #214，`全球五地R1-R5投资风险分级收益测算（史实版）.md`）：Markdown 表格型，`## x、欧洲/英国/美国/香港/中国市场（…）` 节标题定地区（country 键不变），仅 `### x.4 逐年收益率（%）` 表入库（表头按列名定位 R1–R5、末列背景文本忽略；x.5 分阶段复合年化、§六 横向对比、0.2 史实验证表均排除），数值为不带 % 的百分数；② 旧分地区格式（年份标题 + 逐行 `- R1：x%` / 竖线 `R1：x｜…` pair，#163 集满五档封盘、#184 带 % pair 视为附录跳过）。writer 为 **upsert**（issue #214）：新键插入、同键 rate/source_file 有变则 ORM 更新（rate 列 Numeric 读回 Decimal，比较前转 float）、无变化不写——整合文件取代时 1050 行溯源刷新、未来史实数值修订重跑 ingest 即落地。
- **initial_asset**：初始资产（现金/债券/股票/房产）→ 存量建档 `initial_asset`；现金进银行、股票债券一组、房产一组。
- **basic_income（issue #211，整合旧 income_* 四类）**：`基本收入.md` 按人分节（`## 一~四、`；第五节汇总表不导入），节内 x.1 股债表（债券收益/股票收益两列）、x.2 房产表（惠民租房/经营性房产；Henri 为 惠民(祖父)/(先祖)、经营性(祖父)/(先祖) 四列）、4.3 商业表（税后落袋）。年份格支持单年与段（`YYYY–YYYY`，en-dash/hyphen 兼容，段内逐年同值展开）；币种格 `NLG/年` 剥后缀识别，Henri 1974–2001 `BEF/LUF` 双币格按列拆——祖父两列 BEF、先祖两列 LUF（1:1），2002 起全 EUR；0 值不产行（惠民 2008 起归零，issue #28）；`合计` 列做分量对账（不符 → warning）。记录自带 stream_type/group_key/label/currency/year/amount/source_line，writer 仅做 holder 归一（TITLE_ENTITY）+ 落 `income_stream` + `finance_entry` 镜像。旧四个配置推导型 parser/writer 与 `app/core/factors.py` 分段复利因子已随本变更删除。
- **salary**：养父/养母薪资 → 取文件税后值 → `income_stream(salary)` 挂其账户；跨币种按文件币种进各自本国货币池。issue #220：① 数据源为 `养父/养母的薪资_CNY修正版.md`（中国任职段结算币种 USD→CNY：养父 1998–2012、养母 1991–2012；退休奖金为表外文字段，历来不入逐年表）；② parser 币种列名改包含匹配（「结算币种」等带前缀写法同样定位）——修复养父表头「结算币种」未命中致 1989 起美国/中国段全部错标 BEF 入库的既有 bug；③ writer 改**按人替换式**：文件即该人入职至退休全量台账（salary 流唯一写入方），导入前先删该 entity 旧 salary `income_stream` + `finance_entry` 镜像（不限 source_file）再插，文件名更替/口径修正时老行自然清场、不会同年 BEF/USD/CNY 双份，二跑幂等；④ 替换语义下 salary 不再走 H2 金额冲突拦截（旧值被权威整段覆盖，仅保留 H1 时间线 soft 提示），`--force`/`force_files` 均可安全重导（#114「salary 不支持 force」约束退役，salary 不触碰 ledger）。issue #222：⑤ 表外「退职金专项核算」段一并解析——退休年一次性税后退职金（比利时 Assigned out 口径：2 倍基薪、EUR、18% 优惠税率；养父 732,826 EUR、养母 747,584 EUR，均 2012），取「税后退职金」行 `**bold**` 段金额+币种（bold 优先以避开同行税前数，无 bold 兜底行末），年份取段标题 4 位年；记录标 component=severance，并入 salary 流（group_key/label 用「退职金」，与同年 CNY 薪资币种不同天然共存），替换清场覆盖薪资+退职金两类镜像。
- **household_expense**：家庭支出 → 取"年度总支出"行 → `ledger` 支出（挂 Henri Peeters 账户）；2002 起停设。parser 按首格「年份」+「总支出」表头定位逐年表（issue #24），修正版（issue #216）前置的计列项目/CPI 验证/累计等表首格非「年份」天然跳过；记录带 source_file。writer 为 **upsert**（issue #216）：(account, date, reason='家庭支出') 同键行金额/来源有变则更新 ledger 并同步 finance_entry 镜像、无变化不写——修正版替换或未来金额修订重跑 ingest 即落地（旧语义下金额修订会同年插入第二笔支出）。
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
收益文件(基本收入.md：股债/租/经营性房/开店逐年终值，issue #211) + 薪资 → 逐年现金流 → 挂对应 entity → 进账户活期
支出文件(家庭支出) → 挂 Henri Peeters 账户 支出
```

- **ingest 顺序锁死**：`人物/` 先入 `entity` → 初始资产 → 收益文件(@entity_id) → 银行。收益模块挂 `entity_id` 依赖人物先导入。
- **归属主体来源 = 人物档案 `entity`**；收益模块按**属地/地域颗粒**拆分，各挂各主体；归属名 ↔ `entity.name` **失配进 `ingest_report` 标需人工**（不静默跳过，复用 `date_rule` 那套"补一条沉淀复用"）。
- **收益文件 = 模块化**：一个文件含多个属地子模块（如经营性房产收益=比利时/丹麦/荷兰…每模块一个补属主体）。模块即挂账单位。
- **股票债券归属粒度**：按**地域颗粒**打包挂一个人/公司（如"丹麦的股票债券"一个包挂某主体）；**包内收益仍分开计算**（每只股/每支债各自算票息），属同域同主体。
- **房产**：房产A/B/C 全算收益（经营性房产收益 + 惠民租房）；家庭主古堡也列收益。
- **薪资/收益文件已写死金额/税后/币种**：系统**直接取文件金额入账，不重算税率/CPI/人口分段**（文件即权威，系统只搬运+校验，保"数据算得平"）。
- **实现注记（issue #211 取代 issue #69 旧链路）**：五人初始资产逐年收益已整合为 `基准/收益表/基本收入.md`，**逐年终值直入** `income_stream`（股债 security / 惠民租房 rent / 经营性房产 property / 祖父开店 shop；因子 A「文件终值权威」，issue #114）。旧的「基桩值 + 分段复利在 ingest 时展开」链路（惠民租房/经营性房产/祖产股票债券三文件 + `app/core/factors.py`，issue #69）与开店时段均值 parser **已全部删除**；旧四个源文件于 2026-09 从 Design_Folder **彻底移除**（detect 的 SKIP_SUPERSEDED 规则保留为护栏，yaml 同步除名）。Henri 房产表「先祖」列为卢森堡 LUF 资产、「祖父」列为比利时 BEF（1974–2001，1:1），均挂 Henri Peeters 一人；2002 起统一切 EUR。2008 起惠民租房记 0（无行）、经营性房产为扣全板块人工成本后净额。薪资/家庭支出仍直读文件逐年值。
  > ⚠️ 数据对账（2026-09 导入时）：`基本收入.md` 第五节「全周期累计汇总」与其明细表在**房产类 6 个口径**上不一致（如 Henri+先祖 房产 BEF/LUF 汇总 81,756,443 vs 明细 236,800,383；养外祖父 房产 EUR 汇总 7,087,075 vs 明细 4,256,896），股债/商业有 ±1~8 舍入级差异；导入以**逐行明细终值**为权威（每行 `合计` 自洽零告警），汇总表待数据调整员在文件侧订正。
- **起始现金进余额**：初始资产里的**现金 = 直接进对应账户银行作为初始余额**，是后续"钱→收益/投资"的本金种子。
- **薪资/收益各归各主体账户**：养父薪资 → 养父账户、养母薪资 → 养母账户；收益各进各主体账户；**家庭支出统一记 Henri Peeters 账户**（故事设定，非分摊）。
- **支出**：家庭支出取"年度总支出"行入账（不重算人口分段/CPI）；**2002 起家庭支出设定停止**（无此文件后续）。
- **投资手动推进**：钱→收益测算表投资（R1–R5）→ 年末赎回分红进银行 → **次年需用户手动重投（非自动续投）**，可改地区/R/金额或转投股票/电影（Phase2）。

### 6.6 币种生命周期与池关闭（Phase 1/2 通用）

- **账户两级模型**：人物/公司 = 大账号(account) × 其下币种资金池(account.currency)。两级都可整体关闭。
- **BEF/LUF/NLG：2002-01-01 关闭** → 池进入只读终态（不可再存/投/换汇），历史流水可回溯展示；`EUR` 池开一条承接分录（从 BEF/LUF/NLG 划转 XXXX，带原币、金额、折算汇率），之后新流水在 EUR 池。收益文件已内置 EUR 金额。
- 含币种生命周期的转换，系统与文件统一（文件已写死 EUR，系统不另折）。

### 6.7 持有人 → 币种/实体映射（holders.py，实现补记 issue #145）

`app/ingest/holders.py` 是 CLAUDE.md「币种铁律」的代码化锚点，DESIGN 此前未记载：
- `HOLDER_CURRENCY`：持有人登记名 → 允许币种组（祖父=BEF+LUF、祖母=SEK、外祖父=NLG、外祖母=DKK、养父母=BEF）；匹配规则「先精确、后前缀」防 养外祖父≠祖父 误吞。
- `TITLE_ENTITY` / `holder_entity_name()`：中文职称 → 规范 entity.name（养祖父→Henri Peeters、养父→Joren Peeters…）。
- 消费方：initial_asset/bank 解析器的币种归组、conflict `_resolve_entity_id` 归一、**writer 侧收益/薪资/银行落账的实体归一（issue #136）**——writer 现统一经 TITLE_ENTITY 归一后再 upsert，杜绝「收益挂职称别名实体 ≠ 账户挂规范实体」的账务锚分裂；存量别名实体用 CLI `merge-alias-persons` 合并（含「职称（资产归…）」括号注解变体，精确键+注解形态，不做通用前缀模糊）。

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
- 默认风险等级 **R3**（家族主仓口径，`app/core/regions.py:DEFAULT_RISK_LVL`；可被 `entity.fields["risk_lvl"]` 覆盖——issue #145 补记）。
- 逐账户逐年滚动：`balance_y = balance_{y-1} × (1 + rate_calc) + 净流入`。滚动区间上限收敛 `config.CALENDAR_MAX_YEAR`（issue #141）。
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
      → 实现注记（issue #140）：run_report/summarize 支持 from_year 范围化
        （H1/H2/H4/负余额只报该年及以后；H3/H5/H-STOCK 全局完整性规则不受限）；
        findings（crit 优先，截断 20 条）持久化进 recompute-done notification.payload，
        自身异常记 payload.health_error，不再静默吞掉。
3 写 recompute_job(status=done, start_year) → 建 notification(recompute-done)
4 返回 job_id：前端据此弹「全局重算完成」非阻断提示，「查看影响」跳健康校验屏 /
   在横幅内展开受影响明细（§9.3，issue #140 前端补齐）
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

> 实现注记（issue #145）：H2 基础版（`check_h2_amount_consistency`）仅做 income_stream 同
> (entity, stream_type, label, year) 多来源金额比对；「台账 vs 事件 vs 收益表按 company/ticker 聚合」
> 的 company 维度由 `check_stock_h2`（F-P2-04）单独承担，两函数分工覆盖 §10 全语义。
> 另有表外补充规则见 §21.4。

- 结果供两角色查看；可作为导入后「数据状态是否正确」的核对依据。

---

## 11. 文件变更流与回退

### 11.1 新增文件（数据调整员）
1. **激活（仅 prod）**：在 `Design_Folder/import_files.yaml` 把该文件置 `active:true`（严格白名单，§16）；dev/test 全量导入、无需登记。
2. 放入/更新 `source_dir` → 运行 `python -m app.ingest.main ingest --env <env>`
   （prod 只导入激活项；`--force` 跳过指纹 gate 重浇灌四类收益文件，issue #114）。
3. detect → parse → normalize → 事务导入 → 增量重算 → notification。
4. `source_file_version` 记录 `is_current` 的新版本。

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
- 例外通道：仅管理员手动维护窗口可触发删除（实现为环境变量 **`ALLOW_ADMIN_CLEAN=1`**——issue #145 措辞更正：非 CLI 标志，语义等价「开发时定」的管理员窗口）；普通流程永不调用。
- 唯一删除入口：`DELETE /api/v1/entities/{id}` 仅在 `ALLOW_ADMIN_CLEAN=1` 环境下放行；非该模式的普通流程对该端点一律 409。
- 每个公司带状态字段 `status`（取值开发时定）；source 标识 = `'external-api'`。
- 触发：UI 用户在公司图谱屏点「获取/导入」按钮。

### 13.2 API② 用工成本计算
- 输入：本地**逐年「用工成本 + 税率」基准**（issue #218 起为 `基准/公司/用工成本/` 下 **2 个汇总文件**：`用工成本汇总_12地_CPI修正版.md`（12 地区 × 年，9 列同构表：人均年薪/涨薪幅度/币种/CPI 定基 2013=100/投资金融年薪…）与 `各国雇主社保税率汇总（逐年展开版）.md`（`### N. 地区` 11 节覆盖 12 office，上海节兼落外籍）；原 10 国工资分文件 + `基准/CPI工资.md` + `税率/` 12 分文件共 23 个已删除并入）。
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
- **实现（API① · F-P1-05）**：`app/ingest/importers/company_info.py`（login→fetch `/public/companies`→按 `(entity_type='company', name)` 只增不减 upsert，source=`external-api`；股权结构→公司/自然人股东建实体 + `rel_type='holds'` 边；开停业日期/外部ID/持股比落 `entity.fields` JSONB）。凭据 `secrets.local.yaml` + 环境变量覆盖，URL per-env 默认 dev/test 7273、prod 7274。触发 `POST /api/v1/graph/companies/import`。
- **外部 API v2.6 适配（2026-08-26）**：① `/public/companies` 新增 `tax_zone_id/tax_zone_label`
  （公司↔税区一对一，内部成本口径键）随 fields 落库备查；② `/public/positions` v2.6 R2
  「计算权移交第三方」与现架构一致（外部只给明细、本地基准自算成本），新增
  `unknown_levels` 统计防未知 level 静默 0% 低估；③ `/public/levels` 字典端点暂不接入
  （LEVEL_PCT 为本方费率映射表，外部字典仅编码展示）；④ 403 权限语义/429 限流由既有
  upstream 错误映射覆盖。
- **实现（API② 用工成本 · F-P1-10）**：`app/core/labor_cost.py`（本地基准 + 税率公式执行器 + 逐岗位成本；Level/外包/晋级规则表，也是「加薪规则」屏数据源）+ `app/ingest/importers/positions.py`（`GET /public/positions?year=` 拉流入岗岗位 → 逐岗位算成本 → 每公司×年 `finance_entry(entity_kind='company', kind='expense')` 落账）。基准三表 `labor_wage_benchmark / labor_cpi_growth / labor_tax_benchmark`（`python -m app.ingest.main labor-baseline`；issue #218 起 `app/ingest/labor_baseline.py` 直读 2 个汇总文件，**替换式**落库——三表唯一写入方即本 CLI：工资按 `## <地区> ·` 节切分（`全周期关键指标` 等非地区节排除）、CPI 同比由定基指数相邻年反推（首年 None）；税率按 `### N. <地区>` 节切分、区间年（1982-1983/2017-2025）逐年展开、双值格取末值（英国 2025 年中改革）、上海节兼落「中国上海外籍」、节底文字常数入 params（荷兰假期 8%、美国 FUTA 工资基 $7,000、英国职业养老金 2012 起 3%）。工资地区由 10 区改 12 地（美国拆纽约/洛杉矶、新增北京），`LOCATION_ALIAS` 同步：纽约/洛杉矶各取本城、北京不再代理上海）。税率公式细节只在后台（含说明文字隐藏项：比利时 CP200 十三薪/双倍假期、英国学徒税 >£3m、日本固定奖金 3 月等）；UI 只展示加薪规则。端点 `POST /labor-cost/compute`、`GET /labor-cost/rules|results`。

---

## 14. API 端点清单（严格 RESTFul，FastAPI）

### 14.1 REST 约定
- **资源名词**：URL 用**复数名词**（`entities`、`timeline-events`、`finance-entries`）；动作以 HTTP 动词表达。
- **HTTP 方法**：`GET` 读取 / `POST` 创建 / `PUT` 全量替换 / `PATCH` 局部更新 / `DELETE` 删除；幂等性按 HTTP 语义。
- **状态码**：`200 OK` / `201 Created` (+ `Location`) / `204 No Content` / `400` 参数错 / `404` 资源不存在 / `409` 冲突（如幂等键冲突）/ `422` 校验失败 / `500` 服务器错。
- **集合**：支持 `?filter=`、`?sort=`、`?page=`、`?page_size=`、`?as_of=YYYY-MM-DD` 查询参数；分页默认 `page_size=50`。
  > 实现注记（issue #156 备案）：本地单机低频场景下，部分列表以具名过滤替代通用
  > `?filter=`/`?sort=`；jobs 用 `limit+offset`（默认 50）、notifications 用 `unread_only+limit`
  > （默认 20），movie-events / stock-events / labor-cost results / source-files 暂不分页。
  > 核心大集合（entities/accounts/ledger/timeline/finance/returns/fx 等）均按 `page/page_size` 默认 50。
- **子资源**：父子关系用嵌套 URL（`/api/v1/source-files/{id}/versions`），不暴露动词。
- **异步动作**（import / recompute / export）建模为 **job 资源**：发起即创建 job，子操作变成对该 job 的状态查询（避免 RPC 风格 `/run`、`/accept`）。
- **视图资源**（多资源聚合）以 `/api/v1/overview`、`/api/v1/graph/{kind}`、`/api/v1/wealth`、`/api/v1/returns`、`/api/v1/finance` 提供，只读 GET。
- **URL 版本段**：所有内部 API 一律以 **`/api/v1/`** 为前缀；后续不兼容演进发布 v2 时**并行运行 v1**（v1 进入维护期，仅修 bug、不加新端点），客户端可显式指定版本。
- **写端点授权**：除 `timeline-events`（编年史，经覆盖层）与 `source-files/{id}/versions*`（diff 决策）外，其余**写端点**（创建/全量替换/局部更新/删除 `entities`、新增 `ledger-entries`、新增 `finance-entries` 等）**不面向普通 UI 用户**，仅供 importer / 数据调整员（受限通道，对齐 PRD §1.4 铁律）。普通 UI 对这类写端点的调用应由 serve 层拒绝（409/403）。
  > **实现注记（issue #87-4 → 2026-08 审计批回写）**：守卫 `app/api/deps.py:require_importer`
  > （`X-Importer: 1` 放行，否则 403）现挂载于 `app/api/restricted.py` 的 8 个受限写端点
  > （entities 增改删 / relationships 建·删 / ledger-entries·finance-entries 新增），
  > 403/放行双路测试见 `tests/test_restricted_guard.py`（issue #112）。

### 14.2 资源端点

#### 实体 / 关系 / 图谱
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/entities` | 列出实体（过滤 `?type=person\|company\|asset\|family`、`?status=…`） |
| GET | `/api/v1/entities/{id}` | 单实体详情 |
| POST | `/api/v1/entities` | 创建实体（受限通道） |
| PUT | `/api/v1/entities/{id}` | 全量替换（受限通道） |
| PATCH | `/api/v1/entities/{id}` | 局部更新（含 `status`；受限通道） |
| DELETE | `/api/v1/entities/{id}` | 仅 `ALLOW_ADMIN_CLEAN=1` 环境变量通道放行（§13.1）；普通流程 409。注：无 `X-Importer: 1` 时先 403（守卫先于门禁） |
| GET | `/api/v1/entities/{id}/relationships` | 实体的关系列表 |
| POST | `/api/v1/entities/{id}/relationships` | 建立新关系 |
| DELETE | `/api/v1/relationships/{id}` | 删除关系 |
| GET | `/api/v1/graph/persons` | 人物图谱视图（只读；issue #156 记法更正：斜杠路径，非点号） |
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
| GET | `/api/v1/returns` | 收益曲线列表（国家 × 风险级 × 年份；视图资源命名从 §14.1） |
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
| POST | `/api/v1/source-files/{id}/versions` | **采纳新版本**（实现备案 issue #142/#145：无请求体=采纳磁盘当前版，返回 200 非 201——对 §11.2「body=新内容+201」的有意偏离，本地单机以磁盘为新源） |
| POST | `/api/v1/source-files/{id}/versions/{vid}/restore` | **回退到指定版本**（DB+磁盘复原） |

#### 导入 / 重算（异步 Job · issue #138 方案A 已实现）
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/import-jobs` | 触发外部导入任务（202；body：`{provider:'company-info'\|'labor-cost', payload:{...}}`） |
| GET | `/api/v1/import-jobs` | 任务列表（过滤 provider/status） |
| GET | `/api/v1/import-jobs/{id}` | 任务详情（含 result/error） |
| DELETE | `/api/v1/import-jobs/{id}` | 取消待执行任务（仅 pending，204；其余 409） |
| POST | `/api/v1/recompute-jobs` | 触发重算任务（202；body：`{start_year?, reason?, files?}`），后台执行后通知附健康摘要 |
| GET | `/api/v1/recompute-jobs` | 任务列表（过滤 status） |
| GET | `/api/v1/recompute-jobs/{id}` | 任务详情（done 后附 health/health_findings，供「查看影响」） |
| DELETE | `/api/v1/recompute-jobs/{id}` | 取消 pending 任务（204；其余 409） |

> 实现注记：进程内单 worker 串行（threading.Lock）；每任务独立 session；
> 状态机 `pending→running→done/failed`。既有 §21.3 同步 RPC 端点保留（秒级 UI 动作）。
> 取消语义（issue #156 注记）：DELETE pending 任务返回 204 后，任务行落
> `status='failed'` + `error='已取消…'`（无独立 cancelled 终态）。

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

> **实现超集路由（issue #156 收录）**：除上表外，实现另含以下已备案端点——
> `investments`×5（GET 列表/{id}、POST、PATCH、POST {id}/redeem）、`POST /transfers`、
> `POST /demand-interest`、`movie-events`×4（GET×2 + link/unlink）、`stock-events`×7
> （events/positions GET ×2 + associate/buy/sell/dividend/passive-uplift POST ×5）、
> `labor-cost`×3（compute/rules/results）、timeline overlay 扩展×3（diff/merge/source-as-latest）、
> `GET /returns/countries|regions`、`GET /graph/all`（issue #84）、
> `GET /entities/{id}/finance-entries`、`GET /source-files/{id}/meta` 别名与 `/{vid}/diff`、
> `GET /ingest-reports`（issue #118/#123）、`GET /ping`、`POST /graph/companies/import`
> （四轮审计 #171 补录；语义见 §13.3/§21.3 同步 RPC 清单）。
> 功能语义分别见 §13/§18/§19/§21.3；`snapshots/{date}` 与 `source-files 单版本内容`
> 两端点已按 §14.2 原表补齐（issue #155）；`GET /exports`（产物清单）为 F-P2-07 实现超集
> （§14.2 原表仅列 POST /exports 与 GET /exports/{id}）。

---

## 15. 导出（export/）

- **markdown**：按当前生效数据（源+覆盖层合并）渲染结构化 md，可作新素材档案。
- **CSV**：财务、收益、持仓等表格。
- **PDF**：报告（图表内嵌）。
- 原则：仅导出，不写任何源/输入文件。

---

## 16. 三环境 / 迁移（Phase 2）

- 三库独立；Alembic 迁移共用。
- **prod 门控**：`Design_Folder/import_files.yaml` 激活清单仅控 prod 导入（strict allowlist），dev/test 全量。
- **清库**：`reset --env prod` 删全部 public 表 → `alembic upgrade head` 重建空 schema（保留 pgvector / 库本体 / DSN，不可逆）。
- Phase 2 需求：跨环境数据同步/迁移（如把 test 修正同步回 prod）仍未引入；2026-08-27 起当前阶段为 **Phase 2+：加强 Phase 2 实际使用体验与排错**（激活清单门控、reset 清库、启动脚本、事件/股票 UI 交互等运维/实战收尾）。

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
  > **实现注记（审计修复补齐）**：活期结息落地 `app/core/demand.py`——每年 12-30 对全部
  > active 账户按台账逐段余额加权 Σ(余额×持有天数)×2%÷365 计一笔 `ledger_entry(kind='income')`
  > 入账 + `finance_entry(source='ui')` 镜像 + 编年史 overlay 直写（issue #86 同款）；
  > note/label 打 `demand#{year}` 标签、同年重跑幂等整笔覆盖；未到结算日 422；
  > 负余额不罚息、0 利息不生行。API：`POST /api/v1/demand-interest`；UI 入口在投资屏。
- **专款池（年度投资资金，分币种）**：投资本金=从银行 `ledger` **划出**入池（银行-）；赎回/补活期=划回（银行+）；**专款池本身不直接记支出**。支出只在银行账户发生：活期不足→先从专款池划回活期→再从银行支出。
- **走账模型**：银行 `ledger_entry` 承担全部收支（划出/划回/支出），保 `后一=前一+入−出`；专款池是银行的"在途仓"，非跨年持仓（年末清空）。对应 `finance_entry`：划入=kind'pool'（或投资本金）、年末收益=kind'investment_income'。

### 19.3 校验（服务层，UI 派生通道的瘦版 hard-block）
- 金额 ≤ 该币种 as-of 余额（422）
- 地区起始年下限（欧洲1947/英1983/美1989/港1999/中2002）— UI 选项下限 + serve 校验（422）
- 每「年份+地区」一年一次幂等（409）
- 覆盖连锁：若前序改动致后年「全部」超新 as-of → **整体拒绝该改动**（保守挡停，不自动压缩、不追手动重输）

### 19.4 重算与净值
- **重算**：向后最小传播；固定金额（`is_all=false`）冻结不追溯；仅「全部」（`is_all=true`）与 as-of 变更向后传播（因每年末重置，传播最长相邻年）。
  > 实现注记（issue #145）：当前实现为粗粒度 `recompute_all(from_year)` 全账户重算——
  > 结果等价（无收益率的账户滚动不变），以简单性换精度，非字面逐 alloc 最小传播。
- **净值**：财富曲线 = 银行 + 专款池合计；投资期间（发生日~12-30）总净值**不变**（资金在途不单独下沉），仅年末结算收益（含负收益）才令总净值变化。

### 19.5 划拨 / 换汇（P1，UI 类操作）
- **触发**：输入 **年份** → 选账户 → 源币种资金池 → 目标币种。同币种移动 = 划拨（净额 0，改 ledger 两笔同币一进一出）；跨币种 = 换汇（需 `exchange_rate` **该年份**汇率）。
- **gate**：换汇命中该年汇率才可换；缺则拦，数据调整员按需导入该年汇率后再操作；不做全时段穷举。接受**反向汇率行**取倒数（issue #87-1，§21.3 补记）。
- **校验**：**转出年起向后全链 as-of 不得为负**，任一年拐负 → 整体拒绝转出（同投资选 i 的整体拒绝，复用同一套向后重算传播校验）。划拨目标可为另一实体账户（人物 A→公司 A）。
  > **口径注记（issue #93）**：破负预检 `transfer._simulate_annual_asof_nonneg` 采用**逐年 12-30（年末）as-of 滚动**口径，非逐日连续——年中短暂拐负、到年末回正的场景不会被拦截。这是对「向后全链 as-of」的**有意降级简化**（与 `snapshot._account_balance_series` 同源逐年口径一致，代价低、无逐年跳变语义）。如需逐日严格预检，可复用 `invest._simulate_outflow_nonneg` 的按 ledger 行滚动改法；当前按年口径已满足转出守门需求，暂不升级。
- **记年**：锁输入年份；落 `ledger_entry` 后传重算；同步编年史 overlay。

### ★ 实现决策备注：UI 派生编年史直写 overlay（issue #86 定案）

UI 派生操作（投资创建/赎回、划拨换汇、活期结息）的编年史同步为**直写 `timeline_event(overlay=True)`**，
**不经** `user_data_overlay` 表 + `overlay_dir/编年史.md` 覆盖层文件通道（§6.4）。
实现位置：`app/core/invest.py`（创建 §19.1 / 赎回 §19.2）、`app/core/demand.py`（活期结息）、`app/core/transfer.py`（§19.5）。
定位标签约定（issue #137 词边界加固后）：抹除/重放以正则 `标签(?!\d)` 精确匹配——
`inv#1` 不命中 `inv#10~19`；demand#{year} 同函数复用。

- **理由**：派生行由系统生成、无用户自由文本编辑需求；timeline ingest 为 insert-only 幂等键
  `(event_year, title, source_file)`（`app/ingest/writer.py`），重导不会冲掉这些行，数据安全可接受——
  是对 PRD §1.4「界面更新经覆盖层」字面约束的**有意偏离**（架构决策），据此固化为文档基线。
- **后果**：§6.4 覆盖层能力（覆盖层 md 导出 / 差异 / 重置回源 / 以源为最新）对这批派生
  `overlay=True` 行**不适用**。P2-05「编年史 UI 编辑」落地时须明确处理策略占位：视为**系统行
  只读**（落库按 `note` 标签 `inv#<id>` / `UI 转移` 定位），用户编辑一律走真实覆盖层通道。

### 19.6 事件·股票与资产模型（Phase 2）
- **导入链**：`基准/事件/股票/**` Phase 2 重新纳入 detect → 数据调整员导入**不关联账户** → UI 用户按**同币种**手动关联到某账户(人物/公司)；现金流经事件生成 `ledger_entry` 进账户。
- **总资产**：`银行账户余额(现金) + 投资专款池 + 股票持仓市值`；现金与市值不重叠（买入=ledger 支出「购入股票」移除现金）。
- **持仓**：`holding_event` 需 **batch 维度**（每批买入=一个 batch，各自成本）。分红=每股派息×加权平均持仓→现金收入进账户（口径备案 #166：实现为**事件日当前 open 持仓合计**近似——单批持有下与加权平均等价；事件日期缺粒度按 §6.2 归一）。被动抬升占比（回购缩股本）持股不变、无现金动作。
- **成本 = FIFO**：卖出/减持从最早批次扣成本，`盈亏=卖出现金−批次成本`，进账户；成本仅供卖出结算，不参与总资产。
- **并购三形态**：
  1. 纯换股/分拆（HPQ→HP+HPE；2DXC→1PRSP）→ 只改持仓、成本按比分摊、现金不动。
  2. 换股+现金（MVL→DIS：1股=30现金+0.7452DIS）→ 换股部分成本随链(MVL成本→DIS批次)；现金部分进余额单独核算。
  3. 纯现金并购（Perspecta→Veritas）→ 持仓归 0、现金进余额、**不记盈亏**。
- HP_CSC 链（HPQ→DXC→PRSP→HPQ 逐年减持）经规则核验闭合；数值逐行对齐待 Phase 2 实际导入后由 §11.4 + H2 验证。
- **「持仓市值」口径注记（issue #145）**：当前实现为**成本基准值**（unit_price=买入成本），
  非 mark-to-market 市价——stock_wealth.py docstring 已声明；市价估值留待有行情源后增强。
  另 snapshot 的 `entity:*` 层仅当币种=USD 才并入持仓市值（第一阶段全 USD 限定）。

---

## 20. 开发功能清单（按优先级，供跟踪）

> 状态图例：⬜ 待做 · 🟨 开发中 · ✅ 已完成 · ⛔ 组内置灰（暂不做）。勾选随实现推进更新。
> **编号**：`F-P0-xx`（Phase1·P0）、`F-P1-xx`（Phase1·P1）、`F-P2-xx`（Phase2），供文档/需求/任务引用。

### Phase 1 —— P0（核心可用）

| 编号 | 模块 | 功能 | 关键章节 | 状态 |
|---|---|---|---|---|
| F-P0-01 | **工程骨架** | config 三环境 / db / alembic 迁移 / ingest CLI | §3–§4 | ✅ |
| F-P0-02 | **Ingest** | detect 类别识别 + 解析器(bank/股票表/汇率/人物/时间线) + normalize｜cpi_wage→SKIP_PARAM、stock_tx 显式 Phase2 跳过 (#70) | §6 | ✅ |
| F-P0-03 | **Ingest** | 导入前冲突检测 hard-block（conflict.py，§11.4 + 金额/余额等）｜软硬分级对齐 §11.4：H5/H1=标、H3 链式闭合预检 (#72) | §11.4 | ✅ |
| F-P0-04 | **Phase1 摄入** | 初始资产建档(initial_asset) + 现金进余额｜幂等已验证：文件指纹+自然键双保险 (#68) | §6.5 | ✅ |
| F-P0-05 | **Phase1 摄入** | 收益文件模块化挂账(income_stream)：租/经营性房/祖产债券/开店（薪资在 F-P0-06）｜~~因子外置 core/factors.py，源文件仅基桩值 (#69)~~ **#211 起被 F-P2+-06 取代：逐年终值直入，factors.py 已删** | §6.5/§6.3 | ✅ |
| F-P0-06 | **Phase1 摄入** | 家庭支出(挂 Henri)/薪资各归各账户；2002 BEF/LUF/NLG 关池转 EUR｜家庭支出幂等同 #68 | §6.5/§6.6 | ✅ |
| F-P0-07 | **DDL** | entity/account(status/closed_on)/ledger_entry/income_stream/initial_asset/snapshot 等 | §5 | ✅ |
| F-P0-08 | **快照** | 逐年 as-of 快照预计算（snapshot.py）｜三层 scope + from_year 增量 + pool_in_transit(#85)；date 级按 issue #17 A′ 实时累加口径定稿（§21.5）；真实库联调通过 | §8/§19.4 | ✅ |
| F-P0-09 | **财富曲线** | 账户/币种/公司/全家族合计 + USD 展示折算｜多序列对比前端落地（issue #124）、日历联动（#121）；总资产指标修正为 family:total 口径（#111） | §7/§8/§19.4 | ✅ |
| F-P0-10 | **日历游标** | 全局日历 as-of 拖拽，全 App 联动（服务函数就绪，API 端点 F-P0-13） | §8 | ✅ |
| F-P0-11 | **健康校验** | H1–H5 汇总 + 问题清单（health.py） | §10 | ✅ |
| F-P0-12 | **增量重算** | 受影响起点向后传播 + 提示｜最小传播起点已实现（#120）；blocked 不连坐（#117）；通知附健康摘要（§9.2d）；timeline 编辑触发重建 | §9 | ✅ |
| F-P0-13 | **API** | 基础路由（entities/accounts/ledger/returns/fx/snapshots/overview/wealth） | §14 | ✅ |
| F-P0-14 | **前端骨架** | React+Vite：16 个 tab——Dashboard/投资/划拨/收益/财务/加薪规则/电影事件/股票事件/人物·公司·全图谱(3)/编年史/版本diff/搜索/健康校验/导入状态 + 全局横幅（#122/#140 含「查看影响」）；财富曲线多序列叠加（#143）；tab 计数 15→16 刷新（版本/diff 屏随 F-P2-06 计划内追加） | §1 | ✅ |

### Phase 1 —— P1（完整产品）

| 编号 | 模块 | 功能 | 关键章节 | 状态 |
|---|---|---|---|---|
| F-P1-01 | **投资** | investment + alloc 派生数据、一年一投、R 级计息、年末赎回回银行、手动续投｜core/invest.py，§19.1–19.4 全实现；审计修复：活期 2% 结息 core/demand.py 补齐（§19.2 注记）、批内同池 alloc 累计校验、赎回年末结算日 gate(409)、PATCH 重新锁定 422；issue #93 补 start_date 必须落在 year 年内且 ≤ 12-30 结算日校验 | §19.1–19.4 | ✅ |
| F-P1-02 | **投资** | 专款池（分币种）走账 + 服务层校验（as-of/R起始年/年度幂等/覆盖连锁拒绝）｜§19.3 校验 422/409 + 划出(kind=investment)/赎回(pool+investment_income) 走账 | §19.3 | ✅ |
| F-P1-03 | **划拨/换汇** | 划拨(同币)/换汇(按年汇率) + 转出向后全链不破负拒绝 + 编年史同步｜core/transfer.py，同币两笔净0、跨币缺该年汇率 422；破负预检为年末 as-of 口径（issue #93 §19.5 注记） | §19.5 | ✅ |
| F-P1-04 | **人物图谱** | 人—人/人—公司关系可视化（ECharts graph）｜core/graph.py + SVG 环形布局；issue #84 补 `/graph/all`（按 entity_type 形状/颜色、跨类型边虚线）；**#197 增强**：亲缘**推理边**（app/core/kinship.py，从 entity.fields[「与主角的关系」] 推导夫妻/父子/兄弟/祖先，虚线=建议）+ **图谱内可编辑**（✚ 连线/删/改称谓，删推理写 `infer-suppressed` 标记不复活）+ **点节点看资产**（GET /entities/{id}/assets：账户/初始资产/持仓/收益流） | §1/§14 | ✅ |
| F-P1-05 | **公司图谱** | 公司—公司关系 + 外部 API①② 导入（只增不减 + status）｜**外部 API① 已实现**（`POST /graph/companies/import`，公司图谱页按钮触发，§13.3）+ **API② 用工成本已实现**（F-P1-10，`labor_cost.py` + `positions.py`，公司图谱/用工成本一体化） | §13 | ✅ |
| F-P1-06 | **各国收益曲线** | return_curve（R1–R5）对比渲染 + 地区起始年下限｜GET /returns/regions + SVG 五线对比 | §14 | ✅ |
| F-P1-07 | **财务收支** | finance_entry 各类收入/支出，实体必填、以实体为中心浏览｜真实库验收通过：ingest 后既有 income_stream/家庭支出经 `finance-backfill` 回填 → finance_entry 1485 行（收入1457/支出28，source=file），屏/端点返回正常｜编年史同步口径见 §19 决策备注（issue #86） | §5 | ✅ |
| F-P1-08 | **统一搜索** | LLM+embedding RAG（omlx 本地）条目检索装配 + serve 后处理｜已实现：`search_index` pgvector + 提取器 + `search-index` CLI 索引 + `GET /api/v1/search`（embed→余弦 top-k→LLM 装配→serve 后处理；omxl 未起 503 降级）。EMBED_MODEL 实测 4096 维（ivfflat>2000 不可用→精确扫描） | §18 | ✅ |
| F-P1-09 | 四类 UI 改数据操作 | 统一模板：年份×池 + 后传重算 + 失败整体拒绝 + overlay 同步｜useDataOp 钩子已用于投资/划拨屏 | §6.8/§19 | ✅ |
| F-P1-10 | **用工成本·加薪规则** | 本地基准（工资/CPI/税率）+ 外部 API② 在岗岗位导入 → 逐岗位算用工成本 → 每公司 finance_entry 落账；「加薪规则/用工成本」屏展示加薪规则 + 拉岗位计算 + 结果表｜app/core/labor_cost.py + ingest/importers/positions.py；税率公式细节只在后台（含比利时十三薪/双倍假期、日本 3 月奖金等隐藏项） | §13 | ✅ |

### Phase 2 —— 事件 / 增强

| 编号 | 模块 | 功能 | 关键章节 | 状态 |
|---|---|---|---|---|
| F-P2-01 | **事件·电影** | 电影事件导入 + 不关联 + 同币种 UI 手动关联（现金流入账户）｜已实现：`movie_event` 表 + `event_movie` 解析器（best-effort）+ `ingest events-movie`（8 部入库）+ `GET/POST /movie-events(+/link/unlink)`（关联写 投资出/本金返还/分红 ledger，幂等）+「电影事件」屏 | §19.6/§6.9 | ✅ |
| F-P2-02 | **事件·股票** | holding_event(batch) + FIFO 成本 + 分红/卖出结算 + 被动抬升占比｜已实现 `stock_event` 表 + `event_stock` 解析器(best-effort, USD Style A: 虎牙/哔哩/快手) + `ingest events-stock`(21 条入库) + `stock_cost` 引擎 apply_buy/FIFO apply_sell/apply_dividend/apply_passive_uplift（event_id 幂等、note 打标可撤销）+ `GET/POST /stock-events(+/events/positions/associate/buy/sell/dividend/passive-uplift)` + 持仓市值并入 entity/family 总资产口径(`stock_wealth` 接入 rebuild_snapshots/calendar.snapshot_as_of) + H-STOCK 健康规则 +「股票事件」前端屏 + 23 新单测(block A/B/D) | §19.6/§6.9 | ✅ |
| F-P2-03 | **事件·股票** | 分拆 / 并购三形态（换股+现金/纯现金/纯换股）成本随链｜已实现 `app/core/stock_cost.py` 成本引擎：split_position/cash_share_position/cash_merger + apply_merger(写 holding_event 新批次+结清+现金 ledger，幂等)。形态1 按新股数占比摊成本、形态2 成本全随链现金入余额、形态3 现金不记损益；7 单测复现 UTC 分拆/MVL‑DIS/纯现金 | §19.6 | ✅ |
| F-P2-04 | **事件·股票** | HP_CSC 重组链数值导入 → 依 §11.4 + H2 逐行验证｜已实现 `app/core/stock_chain.py`（apply_chain 按日期驱动 apply_buy/apply_merger/apply_sell 幂等重放 + verify_chain 逐行对账，只读）+ `app/core/hp_csc_chain.py`（HP_CSC_DXC 主链 14 步编码锚定点：CPQ→HPQ→HPE→DXC、CSC→DXC、DXC→PRSP→现金、MFGP→OTEX 现金，as_of=2025 闭合到 DXC 8,782,400/OTEX 1,227,944/三笔现金）+ health `check_stock_h2`（buy 源成本离群 warn / 同日同源单价 crit，排除 split 双源误判）+ conflict `check_stock_event_conflict` 接入 `events-stock`（§11.4 跨文件同键金额不符 hard-block，同源重导入不拦）。模型局限：split 无法「父保留+子另计」，用同名腿近似，只断言股数+现金；链部分数值(DXC 减持等)回测校准标 calibrated。21 新单测，全量 346 passed | §19.6/§11.4/§10 | ✅ |
| F-P2-05 | **时间线/编年史 UI 编辑** | overlay 增改删、差异/重置回源/以源为最新｜已实现 `app/core/overlay.py`（DB-backed 覆盖层 user_data_overlay 为权威 + 合并到 timeline_event(overlay=True)；用户覆盖行 source_file=`overlay:timeline:{key}`，**与 issue#86 系统 overlay 行(source_file=NULL: 投资/划拨/活期)结构隔离只读**；create/update/delete/merge/diff/restore/source_as_latest）+ `app/api/timeline.py`（POST/PATCH/DELETE timeline-events、overlay/restore、overlay/source-as-latest、overlay/diff、合并 GET 每 key 一行覆盖优先，普通 UI 放行）+ 前端「编年史」屏（新增/编辑/删除/差异/重置/以源为最新）。12 新单测(overlay 8 + api 4)，全量 358 passed | §12/§6.4 | ✅ |
| F-P2-06 | **文件 diff 回退** | 版本 diff → UI 决策「更新」/「回退」（DB+磁盘复原）｜已实现 `app/core/versioning.py`（list_tracked/file_diff [[unified diff]]/adopt_current [[复用 import_all(force_files=该文件) 重导入+记版+notification]]/restore_version [[§11.3 安全写盘复原 source_dir + is_current 切换，路径防越权 + 原子写 + notification]]）+ `app/ingest/main.py` import_all/_skip_by_state 加 force_files（版本决策「采纳」强制重导入）+ `app/api/source_files.py`（GET /source-files、/{vid}/versions、diff、POST versions(采纳)、POST versions/{vid}/restore(回退)；普通 UI 放行）+ 前端「版本/diff」屏。**写盘目标偏离**：本仓库实际导入读 source_dir(Design_Folder)，回退写回 source_dir（有意偏离 §11.3 的 input_dir——无独立 input 流）。11 新单测，全量 369 passed | §11 | ✅ |
| F-P2-07 | **导出** | markdown/CSV/报告 PDF（只导出不回写源）｜已实现 `app/export/`（render.py md 全库结构化档案+csv 五 scope RFC4180 / pdf.py reportlab 报告含家族总资产折线图内嵌）+ `/api/v1/exports`×3（POST 201+Location 同步生成、GET 清单、GET {id} 文件流，ID_RE 防穿越）+「导入状态」屏导出中心；产物落 per-env `data/exports*` 不入 git；编年史导出按 §12 每 key 覆盖行优先 | §15 | ✅ |

> 推进原则：先 P0 工程骨架打通 ingest→快照→曲线→健康；再 P1 各交互操作；Phase 2 事件/增强。跨 P0/P1 共用 §6.8 四类操作模板，避免重复实现。

### Phase 2+ —— 运维 / 实战排错（2026-08-27 起）

> 当前阶段：聚焦 **Phase 2 实际使用体验与排错**——生产库由数据整理员按激活清单逐块导入、可清库重建、一键起服务，事件/股票等 Phase2 交互收尾。

| 编号 | 模块 | 功能 | 关键章节 | 状态 |
|---|---|---|---|---|
| F-P2+-01 | **prod 激活清单门控** | `Design_Folder/import_files.yaml` 枚举 Design_Folder 全部 `.md`（含 `基准/事件` 电影/股票及子目录、`基准/公司/用工成本/` 与 `税率/`、模版/设计文件），仅 `active:true` 才经 `ingest`/`events-movie`/`events-stock`/`labor-baseline` 入库（prod 严格白名单）；dev/test 不读清单、全量导入。新文件需登记并激活。已实现 `app/ingest/manifest.py`（load_active_files / require_active_files），四入口接入 prod 门控 | §11.1/§16/§4 | ✅ |
| F-P2+-02 | **清库重建** | `reset --env prod`（`--yes` 跳过确认）：删 `novel_prod` 全部 public 表（保留 pgvector/库本体/DSN）→ `alembic upgrade head` 重建空 schema（不可逆） | §16/§4 | ✅ |
| F-P2+-03 | **启动脚本** | `Design_Folder/start_dashboard.sh`：`APP_ENV=prod` 起动后端 FastAPI(8001) + 前端 Vite(5173)，仅启服务（不含重置/导入） | §4 | ✅ |
| F-P2+-04 | **时间线自动生成默认事件** | `Design_Folder/时间线.md` 清为占位后，时间线改由 `timeline-defaults` CLI 据 DB 自动生成默认事件（`app/core/timeline_defaults.py`）：股票首次建仓(holding_event buy)、影视首次(movie_event)、股票事件首次(stock_event)、每年 R1-5 投资(investment+alloc)；`source_file=derive:timeline-defaults`、overlay=False，幂等合并、`--rebuild` 清旧重建 | §12/§19.1 | ✅ |
| F-P2+-05 | **资产转移** | 图谱资产面板按业务分组（股票债券/惠民租房/经营性房产/现金）把该组 `initial_asset` + 对应 `income_stream`（security/rent/property）改归属到目标 person/company（`app/core/asset_transfer.py`）；普通 UI 可做、服务层校验 422、只改存量不迁历史 ledger、记编年史审计、全量重算(1947)+快照+通知；`POST /entities/{id}/assets/transfer` | §6.8/§19 | ✅ |
| F-P2+-06 | **基本收入.md 合并收益导入** | 五人初始资产逐年收益整合单文件 `基准/收益表/基本收入.md`（股债/房产/商业逐年终值）→ 新类别 `basic_income`：parser 按人物节（`## 一~四、`，汇总节跳过）× 子表（股债/房产/商业）定位列、年份段逐年展开（en-dash/hyphen 兼容）、`NLG/年` 剥后缀、Henri `BEF/LUF` 双币格按祖父 BEF/先祖 LUF 分列（2002 起 EUR）、0 值不产行、`合计` 列对账 warning；writer 落 income_stream + finance_entry 镜像（1117 行）。**完全取代**旧 4 文件（detect 改 `SKIP_SUPERSEDED` 护栏、yaml 除名；旧文件 2026-09 已从 Design_Folder 彻底删除）；旧 income_* parser/writer 与 `app/core/factors.py` 分段复利因子删除；前端零改动（复用四类 stream_type）；dev 端到端对账 H1/H2/H3/H5=0 | §6.1/§6.3/§6.5 | ✅ |
| F-P2+-07 | **全球五地 R1-R5 整合收益表导入** | 5 张分地区测算表整合为单文件 `基准/收益表/全球五地R1-R5投资风险分级收益测算（史实版）.md`（Markdown 表格型）：parser 按 `## x、XX市场` 节标题分流定地区（欧洲/英国/美国/香港/中国 键不变），仅 `### x.4 逐年收益率（%）` 表入库（表头定位 R1–R5 列、背景列忽略；x.5/§六/0.2 表排除）；新旧 1050 个 (地区,R档,年) 键值全量核对**零差异**。writer 由 ON CONFLICT DO NOTHING 改 **upsert**（同键刷新 rate/source_file，数值修订可落地）；旧 5 文件 detect 改 `SKIP_SUPERSEDED` 护栏、yaml 除名并于 2026-09 从 Design_Folder 彻底删除；dev 端到端：1050 行溯源全刷新、二轮幂等 0 写入、H1/H2/H3/H5=0（H4 29 条为 #211 已确认的既有家庭支出口径） | §6.1/§6.3 | ✅ |
| F-P2+-08 | **家庭支出修正版导入并激活 prod** | `基准/1974-2001家庭支出_修正版.md` 替换旧文件（重命名为标准名 `1974-2001家庭支出.md`，detect/yaml 路径不变）：说明章节扩充（1.2 计列项目表 / 1.3 KU Leuven 明细 / CPI 史实验证 / §五关键节点 / §六累计），28 个年度（1974–2001）总支出数值**逐一核对零差异**，parser 无需改（干扰表首格非「年份」天然排除）。parser 记录补 source_file；writer `import_household_expense` 由自然键跳过改 **upsert**（同键 account+date+reason 刷新 outflow/source_file 并同步 finance_entry 镜像；金额修订不再同年插重复行）；yaml 由 active:false 改 **true（prod 首次激活**，Henri BEF 账户 28 笔支出，H4 负余额为收益不入 ledger 的既有口径）；dev 端到端：ledger/镜像各 28 行溯源落库、二轮幂等 0 写入 | §6.1/§6.3 | ✅ |
| F-P2+-09 | **用工成本底稿整合（2 汇总文件取代 23 分文件）** | `基准/CPI工资.md` + 用工成本 10 国工资分文件 + `税率/` 12 分文件（共 23 个）整合为 `基准/公司/用工成本/` 下 2 个汇总文件：`用工成本汇总_12地_CPI修正版.md`（12 地区 `## 地区·` 节，9 列同构表）+ `各国雇主社保税率汇总（逐年展开版）.md`（`### N. 地区` 11 节覆盖 12 office）。`labor_baseline.py` 重写：工资按节切分（`全周期关键指标`/分区标题排除）、涨薪幅度→`wage_growth_pct`、CPI 同比由 2013=100 定基指数相邻年反推（首年 None）；税率区间年（1982-1983/2017-2025）逐年展开、双值格取末值（英国 2025 NIC 15%/£5,000）、上海节兼落外籍 office、节底文字常数入 params（荷兰假期 8%/美国 FUTA cap $7,000/英国养老金 2012+ 3%）；三表改**替换式**导入（唯一写入方即本 CLI）。工资地区 10→12（美国拆纽约/洛杉矶、新增北京），`labor_cost.LOCATION_ALIAS` 同步（北京工资不再代理上海）。旧 23 文件 2026-09 从 Design_Folder 删除；detect SKIP_PARAM/SKIP_P1 规则保留为护栏；yaml 登记 2 汇总文件 active:false（prod 未激活 labor-baseline）。dev 端到端：wage/cpi 各 348 行 ×12 地区（1982–2025 区间）、tax 338 行 ×12 office，二跑幂等 | §6.1/§13.2 | ✅ |
| F-P2+-10 | **薪资 CNY 修正版替换导入（养父/养母）** | `基准/薪资/养父|养母的薪资.md` → `…_CNY修正版.md`：中国段结算币种 USD→CNY（养父 1998–2012、养母 1991–2012）、退休奖金改比利时 Assigned out 退职金口径（2 倍基薪/EUR/18% 优惠税率，表外文字不入逐年表）、养父美国段奖金率修正为 12%；数值自洽（税后=税前×(1−税率)、分量加总、调任汇率换算）逐一核验。parser 修复「结算币种」列头识别（既有 bug：养父 1989 起 USD/CNY 段全部错标 BEF 入库）；writer `import_salary` 改**按人替换式**（先删该 entity 旧 salary 流+finance 镜像再插，文件名更替/口径修正不双份、二跑幂等）；salary 退出 H2 拦截（替换语义，仅留 H1 soft）、`--force` 对 salary 放开（#114 约束退役）；老 2 文件 2026-09 从 Design_Folder 删除，detect SKIP_SUPERSEDED 护栏；yaml 2 新文件 active:true（prod 首次导入薪资 88 行：养父 BEF20/USD9/CNY15、养母 BEF22/CNY22）。dev 端到端：88 行溯源全新文件、镜像 88、冲突 0、二跑 0 写入 | §6.1/§6.3 | ✅ |
| F-P2+-11 | **退休退职金导入（薪资表外专项段）** | 薪资 CNY 修正版表外「退职金专项核算」入 salary 流：parser 定位「税后退职金」行取 bold 段金额+EUR（养父 **732,826 EUR**、养母 **747,584 EUR**，2012 年；比利时 Assigned out：2 倍基薪/EUR/18% 优惠税率），年份取段标题；component=severance，group_key/label「退职金」与逐年薪资区分，同年与 CNY 薪资币种不同共存；writer 替换清场覆盖薪资+退职金两类镜像。dev 端到端：salary 90 行（88 薪资 + 2 退职金）、镜像 90、冲突 0、二跑 0 写入 | §6.3 | ✅ |
| F-P2+-12 | **bugfix：diff 屏残留已取代文件版本（SKIP_SUPERSEDED 版本对账）** | #214 五张分地区 R1-R5 表整合删除后，prod diff/版本屏仍展示这 5 个文件——数据零残留（return_curve 1050 行全部溯源新整合文件），但 `source_file_version` 中 5 条 is_current=True 记录未失活（整合时新文件 upsert 刷新曲线溯源，旧文件版本记录无人收尾；旧文件已从磁盘删除、扫描扫不到）。修复：`import_all` 落库前做版本对账 `_deactivate_superseded_versions`——反向扫版本表所有 is_current 记录，`detect(file_path).category == "SKIP_SUPERSEDED"`（代码层面「已取代」信号，不依赖磁盘存在性）即置 is_current=False 并日志留痕；覆盖 #211/#214/#220 全部护栏路径，现行文件不受影响、二跑 0 失活幂等。dev 端到端：老薪资 2 文件 v1 失活、其余 57 条现行记录不动；prod 5 条 R1-R5 残留待下次 prod ingest 自动失活 | §6.1/§21.16 | ✅ |

### Phase 3 —— 下阶段（暂缓，2026-08-26 起自 Phase 2 移入）

| 编号 | 模块 | 功能 | 关键章节 | 状态 |
|---|---|---|---|---|
| F-P3-01（原 F-P2-08） | **统一搜索增强** | 搜索数据质量达标后再评估是否需要 LLM 判文件兜底｜编号沿用原 F-P2-08 以便引用追溯 | §18.5 | ⬜ |
| F-P3-02（原 Phase 2「视使用情况」项） | **环境间数据迁移/同步** | 跨环境数据同步/迁移（如把 test 修正同步回 prod）——按使用情况再评估，目前未引入，仅说明意图 | §16 | ⬜ |

---

## 21. 实现回写与口径定案（2026-08-26 审计修复批）

> 本节为对照审计（issue #107–#132）后与实现同步的口径/决策回写；正文相应小节如与本节冲突，以本节为准。

### 21.1 计算口径

- **地区→收益曲线国家 = identity**（issue #113）：`app/core/regions.py` 单一权威。源 canon 只有 5 份
  地区测算表，`return_curve.country ∈ {欧洲,英国,美国,香港,中国}`；旧字面量映射（欧洲→比利时等）
  已废弃——曾致收益查询恒 None（投资 422、重算永不复利）。
- **银行台账复利 = opt-in**（issue #113 定案 A）：仅 `entity.fields["compound"]=true` 的账户参与
  §7.2 曲线×杠杆滚动；普通源台账（含自带收益明细行者）文件即权威、纯算术连续（PRD §6.10）。
  覆盖钩子：`fields["return_region"]` / `fields["risk_lvl"]`。H4 经 `_rate_for_account_year`
  同源取率：年内非末条算术连续、年末=年初结转×(1+rate)+净流入、空年照常复利。
- **收益展开因子 A 口径「文件终值权威」**（issue #114）：调价在每年**年初**（含起租年 1974）、
  年末结算入账 → `factor(1984)=1.07¹¹≈2.1049`、`factor(2007)≈5.2100`，与两份收益文件示例逐字一致；
  income(1974)=基桩×1.07。重浇灌命令：`ingest --force`（仅四类收益文件，清旧派生行后重导）。
- **链式折算已实现**（issue #115）：`usd_rate` 直连缺失时经 EUR 枢纽两跳连乘（腿支持反向倒数；
  枢纽白名单 `_HUB_CURRENCIES=("EUR",)` 防任意深度组合），任一腿缺失仍返回 None（宁缺勿错不变）；
  具体年份缺失回退 `year IS NULL` 基准常量；闭合性由 H3/conflict 把关。
- **fx 版本 gate + 权威 upsert**（issue #116）：汇率文件纳入指纹 gate；权威全量表内容变更 →
  同键不同值 upsert 覆盖并记版；非权威文件维持冲突检测+insert-only。

### 21.2 ingest 链路

- **重算不连坐**（issue #117）：hard-block 文件不入库即可，批尾一律对已入库集合执行
  重算+快照重建+通知；通知 reason 标注「部分：N 文件被拦」。
- **最小传播起点**（issue #120）：ingest 按成功导入文件的最早影响年推导起点
  （timeline/shop/bank/rent 取记录最小年；property 固定 1974；全局性类目 → 1947）。
- **ingest_report 落库**（issue #118）：conflict 的 block/warn 与解析失败(error)持久化到
  `ingest_report` 表；`GET /api/v1/ingest-reports` 供「导入状态」屏（issue #123）。
- **date_rule 闭环**（issue #119）：`POST/GET/PUT/DELETE /api/v1/date-rules` 登记；
  ingest 启动装载进 normalize 缓存；默认规则失配或年份带未识别残留文字时消费，
  命中返回 `'date_rule:{id}'`。resolve 仅支持 `'MM-DD'` 字面。

### 21.3 API 契约

- **受限写通道已挂载**（见 §14.1 注记）；403 测试齐备。
- **同步 UI 动作的有意偏离**（§14.1「不暴露动词/避免 RPC」的例外清单，全部为本地单机低频动作，
  不建模异步 job）：`POST /investments/{id}/redeem`、`POST /demand-interest`、`POST /transfers`（issue #145 补记）、
  `/movie-events/{id}/link|unlink`、`/stock-events/associate|buy|sell|dividend|passive-uplift`、
  `/graph/companies/import`（前端 #138 起改走 import-jobs，同步端点保留）、`/labor-cost/compute`、timeline `overlay/restore|source-as-latest|merge`。
  **异步 job 通道已落地**（issue #138 方案A）：`/import-jobs`×4 + `/recompute-jobs`×4 见 §14.2，
  秒级同步动作与 job 化通道并存。
- **契约细节补齐**（issue #127）：timeline-events 支持 `?as_of` 且 page/page_size Query 校验
  （默认 50）；新增 PUT 全量替换；201 响应统一带 Location；entities 支持 `?status=`；
  外部系统错误不透传上游状态码（凭据类→503，其余→502，detail 附 upstream HTTP 码）；
  notifications PATCH 接受 `{"read_at":"now"}`。
- **search 数值铁律落地**（issue #126 · §18.6）：财富意图+年份 → 快照 family:total 确定值注入装配；
  serve 后置校验剔除携带未知数字的整句，全剔则回退「资料未提供相关确定性数值。」。

### 21.4 健康校验（§10 表外补充）

- H-STOCK：持仓 shares>0 缺 unit_price → warn；持仓引用不存在实体 → crit（F-P2-02）。
- check_stock_h2：buy 源成本离群 warn / 同日 buy/sell 单价打架 crit（F-P2-04）。
- 负余额 warn 子检查、汇率覆盖盲区 warn（issue #22 系）。
- H4 实现口径见 §21.1（复利感知双层校验），非纯算术连续。

### 21.5 数据模型补充（接 §5 实现超集）

- 新增表：`labor_wage_benchmark`/`labor_cpi_growth`/`labor_tax_benchmark`（§13.3）、
  `search_index`（pgvector 4096 维精确扫描，§18）、`movie_event`、`stock_event`、`ingest_report`
  （id 用 Integer——量小可接受，issue #145 注记；其余表统一 BigInteger）。
- 列增补：`holding_event.closed_on`（F-P2-03 结清标记）、`investment.redeemed_at`（#82 防重）、
  `labor_cpi_growth.source_file`。
- server_default 补齐：account.status / finance_entry.source / timeline_event.overlay /
  investment.locked / investment_alloc.is_all（e3 迁移）+ **entity.source='file'**（#132/#145 第六项）；
  **finance_entry.source ORM server_default 声明补齐**（#156：DB e3 已有、ORM 漏声明，
  core.py 已对齐）；同批备案：**finance_entry.source 实现收紧为 NOT NULL**
  （设计 DDL 可空 `source TEXT DEFAULT 'file'`，实现 nullable=False + default 'file'）。
- movie_event/stock_event 的 `linked_at` 统一 TIMESTAMPTZ（f2 迁移，issue #145 承 #132 口径）。
- snapshot scope 实现为三段式 `entity:{id}:{currency}`（issue #12），DDL 注释的单段示例作废
  （模型注释已同步更正，issue #145）。
- server_default 补齐：account.status / finance_entry.source / timeline_event.overlay /
  investment.locked / investment_alloc.is_all（e3 迁移）。
- date 级快照按 issue #17 方案 A′ 由 `calendar.snapshot_as_of` 对 ledger 实时累加，
  不预计算落库（§8 相应段落以此为准）。

### 21.6 二轮审计修复批回写（2026-08-26，issue #135–#145）

> 本节为第二轮对照审计（跟踪索引 #146）后的实现/口径回写；与前文冲突处以本节为准。

- **ingest --force / force_files 重导链路修复**（#135）：purge 后必须落入冲突检测+重导
  （合并事故曾致「只删不补」）；--force 对 salary 维持整文件跳过。
  > 注（issue #220，2026-09）：salary 改按人替换式落库后，该约束退役——`--force`/`force_files`
  > 对薪资文件同样安全（writer 先删该 entity 旧 salary 流+镜像再插，不触碰 ledger），
  > 见 §6.3 salary 条与 §20 F-P2+-10。
- **收益归属名归一**（#136）：writer 五个 income/salary 导入 + 银行 + 初始资产统一经
  `holders.TITLE_ENTITY` 归一规范实体；存量职称别名 person 用 CLI
  `python -m app.ingest.main merge-alias-persons [--dry-run]` 合并（含括号注解变体，
  自动重算+重建快照，幂等）——CLI 清单由 13 个增至 **14 个**。
- **派生行标签抹除词边界**（#137）：`delete_derived_by_tag` 以正则 `tag(?!\d)` 复核，
  invest/demand 共用；`inv#1` 不再命中 `inv#10~19`。
- **restore 前置校验**（#139）：磁盘现内容偏离当前生效版 → 409，绝不无提示覆盖。
- **健康复核范围化 + findings 持久化**（#140）：见 §9.2d 注记；横幅「查看影响」入口落地。
- **日历上限收敛**（#141）：`config.CALENDAR_MAX_YEAR = max(2026, 当前年+1)`；
  leverage/transfer/ui_ops/timeline/stock_events/search/snapshot/main 全部跟随。
- **REST 契约**（#142）：POST timeline-events/date-rules 201 带 Location；
  补 GET /ledger-entries/{id}、DELETE /notifications/{id}、GET /source-files/{id}(+/meta)、
  GET /entities/{id}/relationships、GET /holding-events（§14.2 原表资源）。
- **前端**（#143）：Dashboard 多序列叠加（共用标尺+图例）；Invest/Transfer 有意不随 asOf
  联动已在屏注释备案；App.jsx 死代码清理；SourceDiff URL 双问号修复。
- **ingest 卫生**（#144）：run --full 死参数移除；labor-baseline --office 支持 ISO 缩写
  （be/lu/nl/dk/se/uk→中文键）；事件散文件兜底 SKIP_PHASE2_EVENT；主扫描链对 event_*
  显式 ⏭ 跳过；租房展开窗口 (1974,2007)=源文件明文测算周期（注记非遗漏）；
  ingest_report.line 尽力取整（_coerce_line）。

### 21.7 三轮审计修复批回写（2026-08-26，issue #151–#156，跟踪索引 #157）

> 本节为第三轮对照审计后的实现/口径回写；与前文冲突处以本节为准。

- **Dashboard 渲染崩溃**（#151）：`seriesLabel` 引用未声明变量 `series`（#124/#143 多序列
  改造残留死代码），组件每次渲染必抛 ReferenceError——已删除；JSX 实际使用 `seriesLabelFor`。
- **CLI 快照动态上限收尾**（#152，承 #141）：`main.py` 四处 `rebuild_snapshots` 死区间
  `range(…,2026)` 改为 years 缺省走 `config.calendar_years()`（ingest/recompute/snapshot/
  merge-alias 四链路对齐）；前端日历上限改读 `/overview` 新增的 `calendar.{min_year,max_year}`
  （API 未连接回落静态口径）。
- **overlay 子动作触发重算**（#153）：`timeline-events/{id}/overlay/restore|source-as-latest`
  与 `/overlay/merge` 三端点补 `_after_timeline_write`——起点=条目年份（merge 取覆盖层最小
  event_year 兜底 CALENDAR_MIN_YEAR）；source_as_latest 无源(no_source)/merge 无变更时不产生
  冗余 job。
- **§14.2 两端点补齐**（#155）：`GET /snapshots/{date}`（snapshot_as_of 实时累加口径，
  超日历区间 422）、`GET /source-files/{id}/versions/{vid}`（单版本完整 content）。
- **英国学徒税补齐**（#154）：`labor_cost.apprenticeship_levy(salary, p)` 纯函数
  （max(0, 年薪−免征额)×税率，缺省 £3m/0.5%，params `levy_allowance/levy_pct` 可覆盖）
  并入 `uk_nic` 公式第四项；labor_baseline 英国 field_map 补 `学徒税(免征额)` 关键词映射；
  口径注记：按岗位年薪代入同一公式（公司总盘聚合留待真实多岗位数据后增强）；
  rules_payload 不含 levy 细节（§13.2 隐藏项原则不破）。
- **模型/文档卫生**（#156）：finance_entry.source ORM server_default 声明补齐 + NOT NULL
  收紧备案（§21.5 已注）；snapshot.scope DB 列注释更正三段式（迁移 a3b4c5d6e7f8）；
  §14.1 分页例外备案、graph 点号记法更正、§14.2 超集路由收录、DELETE entities 门禁与
  取消 job 终态口径注记。

### 21.8 F-P2-07 导出落地回写（2026-08-26）

- **端点**：`POST /api/v1/exports`（body `{format:'markdown'|'csv'|'pdf', scope?}`；
  同步生成——本地单机数据量小，不建异步 job；201 + Location + body 含 download_url）、
  `GET /api/v1/exports`（产物清单，实现超集）、`GET /api/v1/exports/{id}`（文件流；
  id 凭 ID_RE `[a-z]{3,8}-\d{8}T\d{6}-[0-9a-f]{6}` 校验 + resolve 双保险防路径穿越，
  非法/缺失一律 404）。校验：非法 format 422、csv 缺/错 scope 422、scope 仅 csv 支持。
- **内容口径**：markdown=全库结构化档案六节（实体/编年史/账户/财务计数/收益节选/汇率），
  编年史按 §12 每 key 用户覆盖行优先、系统行与源行保留语义不变；csv 五 scope
  （finance/returns/holdings/timeline/ledger），RFC4180 转义（包裹+内部引号翻倍）；
  pdf=reportlab 报告（摘要表 + family:total 年度折线图内嵌 + 编年史最近 20 条）。
- **存储**：产物落 per-env `config.exports_dir`（dev/test/prod → data/exports-dev|exports-test|exports），
  data/ 已 gitignore；**只读 DB，绝不触碰 source_dir/input_dir**（§15 铁律，测试覆盖）。
- **前端**：「导入状态」屏新增导出中心（格式/scope 选择 + 生成 + 最近产物下载列表）。
- **依赖**：requirements.txt 增 `reportlab>=4.0`（§2 技术栈既定 PDF 选型）。
- **Phase 调整**：F-P2-08（统一搜索增强）自 Phase 2 移入 Phase 3（编号沿用 F-P2-08 → F-P3-01 备案）。

### 21.9 四轮审计修复批回写（2026-08-26，issue #160–#171，跟踪索引 #172）

> 本节为第四轮全量对照审计后的实现/口径回写；与前文冲突处以本节为准。PR-1（P0 #160-#165）+ PR-2（P1-P3 #166-#171）。

**P0（#160-#165，已随 PR-1 合入）**
- PDF 注册 `UnicodeCIDFont('STSong-Light')` 全样式应用 + 测试升级断言字体资源（#160）；
- 换汇正向汇率行 rate>0 校验、0/负视缺失 → 422（#161）；
- currency_from 中文币种词↔缩写配对优先（#162）；
- return_table 每 year 集满 R1-R5 封盘，复合年化附录不再污染末年（#163）；
- 事件关联同币种铁律：movie link / stock associate 服务端 422 + 前端下拉过滤（#164；
  手动 buy/sell/dividend 无事件币种概念不在铁律范围）；
- .gitignore data 段重写为 `data/**` 反白目录与 .gitkeep（#165）。

**P1（#166-#168）**
- event_stock._date_of 缺粒度按 §6.2 归一（年月→月底/年仅→12-30）（#166A）；分红口径
  「事件日当前持仓」近似备案见 §19.6（#166B）；
- 前端健壮性合集（#167）：useFetch 统一 r.ok + 组件内请求序号防乱序；Dashboard 三处
  fetch 补错误态；ImportStatus doExport busy 防重 + refresh 列表；styles.css 补
  .badge/.banner/.diff-add/.diff-del 四类；清理 verMap/RiskLine 死形参/formatNum 重复。
- ingest 卫生九项（#168）：detect「模版/模板」文件名 → SKIP_TEMPLATE；return_table 零条
  落 warning（对照 fx #115）；holders 恒真守卫清理；_CURRENCIES/_CUR_RE 补 NOK/JPY/GBP；
  fx `_cur` 未知币名返 None 宁缺勿错（TSV 直接采信 ISO 代码列）；backfill dup 键补
  currency；ingest_report 同键幂等 upsert（error 行 rule 存 category）；levy 关键词精确为
  「学徒税率」；timeline decade 按行年份推导；parse_number 拒 inf/nan + 支持括号负数。

**P2/P3（#169-#171）**
- core/API 十五项（#169）：invest 负收益赎回拆 outflow；FIFO 浮点残差容差；snapshot 与
  calendar 的 entity 域市值键构建对齐（无 USD 账户的有仓主体也产 entity:{eid}:USD 行）；
  llm 空 data/chat 结构错统一 LlmUnavailable；cancel_pending 纳入 _job_lock；restore 后
  notification 提示重导（磁盘-DB 非事务化语义明示）；check_stock_h2 仅比 buy 间源单价；
  movie link 写账后 rebuild_snapshots；jobs 列表 total=过滤后总数；PUT entities 撞唯一键
  409、POST ledger-entries 先校验账户存在；date-rules PUT 同 pattern 409；source-files
  adopt 异常收窄（业务 422 / 其余 500 通用文案）；export id 碰撞重生成。
- 备案不改码：H3 对 usd_rate 实际生效的 EUR hub 回退链不对账（direct 缺失即 skip——
  hub 链闭合由「宁缺勿错」与 H3 direct 存在时校验共同兜底，§21.4 补充）；
  LLM_MODEL_CONTEXT 为预留配置（omlx 服务端自管上下文，当前未消费，§18.5）。
- 测试缺口（#170）：健康 H1/H2基础/H5 直接测试、外部错误映射 503/502、ALLOW_ADMIN_CLEAN
  正向路径、GET /wealth、pool_in_transit 跨年段、close_2002_currency 承接分录直接单测。
- 文档更正（#171）：§6.3 余额校验位置、§19.6 分红口径备案、§14.2 补录 companies/import、
  本节汇总。


### 21.10 七轮审计修复批回写（2026-08-26，issue #182–#187，跟踪索引 #187）

> 第七轮独立盲审（叠加影响/性能并发/测试质量二遍/验收核对）后的回写；与前文冲突处以本节为准。

- **transfers 幂等**（#182）：transfer() 加 nonce 参数——两笔 ledger note 打 `UI 转移#{nonce}`
  标签、重放词边界复核 skipped 幂等返回；API 层 uuid4 hex 生成。redeem 行读取改
  `with_for_update()` 关闭并发双赎回窗口（SQLite no-op / PG 生效）。
- **return_table 附录 % 特征过滤**（#184A）：格式A 明细行不带 %、「分阶段复合年化」附录行带 %
  ——带 % 的 pair 跳过，杜绝缺档年被复合费率补齐的口径混合（封盘逻辑保留为第二道防线）。
- **movie link 分红-only 快照滞后**（#184B）：_write_movie_ledger 返回 (written, years)，
  rebuild 起点=实际写入流水最早年（消除 today() 回退）。
- **性能形态修复**（#186）：currency.py 抽出 `_rate_from_pairs` 纯函数核心 +
  `rate_loader(session)` 批量预载闭包；wealth_series / rebuild_snapshots 循环内零点查；
  同批修复「NULL-rate 具体年行遮蔽 year=NULL 基准常量」——非正/NULL 行在载入时剔除。
- **fx 源头告警**（#186）：解析层 rate<=0 行不入库并落 warning（ingest_report 可见）。
- **PG-only 冒烟**（#186）：tests/test_pg_smoke.py（无法连 novel_test 自动 skip）——
  import_return_curves on_conflict 真库幂等 + search.retrieve cosine_distance 可编译执行。
- **测试卫生**（#183）：时间炸弹 min(today,2026)→CALENDAR_MAX_YEAR；jobs/search 两处
  monkeypatch 还原；labor results/finance filter 断言补强；snapshot 注释修正（issue #28
  零值跳过语义下条数相等不成立）。
- **docs**（#185）：PRD 屏数条款更正+差异备案、CHANGELOG 重复标题去重、DESIGN↔mockup
  互引补齐、CLAUDE.md 写作线措辞限定。

### 21.11 八轮及九轮审计修复批回写（2026-08-26，issue #189-#193 及九轮 N1-N3，跟踪索引 #191）

> 第八轮独立复核及外部 API v2.6 适配后的回写；与前文冲突处以本节为准。

- **transfers 幂等 API 链路可达**（#189，七轮 #182 缺陷修复）：`TransferIn` 增可选 `nonce` 字段透传；`Transfer.jsx` 表单级幂等键（挂载生成、成功后重置——双击复用同 nonce 第二次 `skipped`）；`post_transfer` 中 `skipped` 时 short-circuit 跳过 `_after_ui_write`（无写入不重算/通知）；前端「重复提交已跳过」提示；`transfer` 正常路径响应补 `skipped:false` 保持契约一致。
- **tax_zone 重导覆盖修复**（九轮 N1）：`company_info.fields` 改条件写入——仅当载荷显式携带 `tax_zone_id/_label` 键时才写入/覆盖，旧版响应缺字段时保旧值（对齐「只增不减」语义，与 `shareholders_pct` 守卫一致）。
- **level 空串静默修复**（九轮 N3）：`positions` 中 `lvl` 为 `None/""` 的岗位此前双重静默（既按 0% 计成本又不进 `unknown_levels`），现显式记为 `(empty)` 入统计可见化。
- **死代码清理**（九轮 N3）：`Transfer.jsx:57-58` 不可达 `else if (skipped)` 分支移除；`currency._direct_rate/_pair_rate` 旧 SQL 点查路径删除（已由 `_load_pairs` + `_direct_from/_pair_from` 批量路径完全替代）；相关 `or_` 导入清理。
- **外部 API v2.6 适配**（#193，PR #194）：见 §13.3 注记；`tax_zone` 条件写入同上。

### 21.12 Phase 2+ 运维回写：数据整理员 prod 工作流（2026-08-27）

> 数据整理员以激活清单逐块控制生产库导入；与前文冲突处以本节为准。对应 §20 Phase 2+（F-P2+-01~03）与 §11.1/§16。

- **激活清单门控**（F-P2+-01）：`Design_Folder/import_files.yaml` 为 prod 严格白名单（`manifest.py`）；
  `require_active_files(env,cfg)` 仅 prod 必读（缺清单报错退出防误全量），dev/test 返回 `None` 全量。
  四个落库入口均按激活集过滤：`ingest`（import_all 过滤 `rep.results`）、`events-movie`/`events-stock`
  （glob 候选按相对路径过滤）、`labor-baseline`（issue #218 起为 import_wage_cpi/import_tax 按 2 个汇总文件激活集过滤）。
  清单枚举 Design_Folder 全部 `.md`（含 `基准/事件` 与 `基准/公司/用工成本/` 汇总文件、模版/设计文件），默认 `active:false`。
- **清库重建**（F-P2+-02）：`reset --env <env> [--yes]` 删 **public 全部表**（保留 pgvector 扩展/库本体/DSN）
  → 设 `APP_ENV` 后程序化 `alembic upgrade head`（migrations/env.py 按 APP_ENV 解析 DSN）；`alembic_version` 随表删除，
  `upgrade head` 自 baseline 整链重建。CLI 清单由 14 个增至 **15 个**。
- **启动脚本**（F-P2+-03）：`Design_Folder/start_dashboard.sh`（`chmod +x`）`APP_ENV=prod` 起后端 8001 + 前端 5173，
  trap 回收后端；仅启服务，不含重置/导入。
- **入库存放**：`import_files.yaml` 与 `start_dashboard.sh` 位于 gitignored 的 `Design_Folder/`，**不入 git**
  （与创作素材同域；工程线代码仍走版本库）。

### 21.13 Phase 2+ 时间线默认事件回写（2026-08-27）

> 手写时间线改为「由导入数据自动生成默认事件」；对应 §20 Phase 2+（F-P2+-04）与 §12。

- **清空手写时间线**：`Design_Folder/时间线.md` 清为占位（保留标题/账户流水规则 + 说明「由导入数据自动生成」）；
  原 decade 表与资产快照删除，`timeline` 类别 ingest 不再从它导入事件 → `timeline_event` 空。
- **生成器 `app/core/timeline_defaults.py`**：`derive_default_timeline(session, rebuild, log)` 据 DB 生成四类默认事件——
  股票首次建仓（`holding_event` buy 按 entity×company 取 MIN(date)）、影视首次投资（`movie_event` per title）、
  股票事件首次（`stock_event` per company×event_type）、每年 R1-5 投资（`investment` per 行，note 附 alloc 合计）。
  事件 `source_file="derive:timeline-defaults"`、`overlay=False`；复用 `writer.import_timeline` 幂等键
  （event_year,title,source_file）合并；`--rebuild` 先删本标记行再重建（不触手工/overlay 行）。
- **CLI**：`python -m app.ingest.main timeline-defaults --env prod [--rebuild]` —— 独立命令、读 DB 非清单门控，
  数据整理员每次导入后运行；幂等重跑只并集。
- **编年史语义**：`timeline_event` 即 UI「编年史」屏所读数据——清空 `时间线.md` 后编年史改由默认事件填充，
  用户仍可走 §12 overlay 在屏上增删改（不改默认事件的 source 标记）。

### 21.14 人物图谱亲缘推理 + 可编辑 + 点人资产回写（2026-08-27 · #197）

> 图谱无边根因：`writer.import_characters` 把「与主角的关系」称谓当目标人名精确匹配 → 全失配。修复为「称谓推理 + UI 可编辑」；对应 §20 F-P1-04。

- **称谓持久化**：`parse_character` 把「与主角的关系」写入 `entity.fields["与主角的关系"]`（upsert merge 回填既有实体）。
- **亲缘推理** `app/core/kinship.py`：`infer_person_edges(session)` 从称谓推导——夫妻(同 anchor 父/母互补、养父↔养母)、父子/母女(「X的父亲/母亲」且 X 在场)、兄弟姐妹、祖先；anchor 经 `TITLE_ENTITY` 解析、主角=Stijn/夏LY；兜底连主角。只推理不写库。
- **图谱边** `core/graph.py`：返回 显式 `relationship` 行(实线, 带 id) + 推理边(虚线, `inferred:true`)；`source_file="infer-suppressed"` 的抑制标记用于隐藏对应推理边（UI 删除不复活；同键显式行存在则不重复出虚线）。
- **图谱编辑 API**（普通 UI 放行）：`POST/DELETE /graph/relationships`（建/删显式）、`POST/DELETE /graph/suppress`（隐藏/恢复推理）。`relationship.source_file` 复用为标记（无迁移）。
- **点人资产**：`GET /api/v1/entities/{id}/assets` 聚合 账户(末余额)/初始资产/股票持仓/收益流；前端 `Graph.jsx` 点节点 → 资产面板 + ✚ 连线/删/改称谓/虚实用例。

### 21.15 资产转移回写（2026-08-27 · #206）

> 图谱资产面板按业务分组把初始资产转给其他个人/公司；对应 PRD §6.8 第 5 类 UI 写操作、DESIGN §20 F-P2+-05。

- **核心 `app/core/asset_transfer.py`**：`transfer_asset_group(session, source_id, kind, to_id, at_date)`——
  按 kind（股票债券/惠民租房/经营性房产/现金）选源实体该组 `initial_asset` + 对应 `income_stream`
  （security/rent/property）一并**改归属**到目标（person/company）；只改存量、不迁历史 ledger；
  写一条编年史审计事件（`overlay=True`「（UI 资产转移）…」）；回退=反向再转。
- **API** `POST /entities/{id}/assets/transfer`（普通 UI 放行）：`TransferError`→422；成功后
  `recompute_all(1947)+rebuild_snapshots(1947)+record_recompute_done('asset-transfer')`+commit。
- **前端**：资产面板每个 kind 分组加「⇄ 转移」→ 填目标 ID → 提交 → 刷新 graph+assets。
- **边界**：kind 判定与展示一致（`_init_asset_kind`/`_income_kind`）；账面现值不重算历史 ledger，
  未来派生收益归新主体。

### 21.16 SKIP_SUPERSEDED 版本对账回写（2026-09-03 · #223）

> diff/版本屏据 `source_file_version` 展示；整合取代批（#211/#214/#220）旧文件从磁盘删除后，
> 其 is_current 版本记录无人收尾，已删文件一直挂为「当前版本」。对应 F-P2+-12。

- **根因**：整合导入只对新文件 upsert（刷新曲线/流溯源），旧文件版本记录不被触碰；旧文件删除后
  扫描链扫不到，指纹 gate 与文件导入状态表均无机会收尾。prod 受影响 5 条（#214 分地区 R1-R5
  表，prod 曾激活）；#211 那批 prod 未激活、无版本记录，故无残留。
- **修复**：`import_all` 落库前调 `_deactivate_superseded_versions(session, log)`——反向扫版本表
  全部 is_current 记录，`detect(file_path).category == "SKIP_SUPERSEDED"` 即置 is_current=False
  并日志留痕（`↻ …文件已被整合取代（SKIP_SUPERSEDED），版本 vN 失活`）。SKIP_SUPERSEDED 是
  代码层面的「已取代」信号，不依赖磁盘存在性；新增整合取代只需在 detect.py 加护栏，版本对账
  自动覆盖，无需额外登记。
- **边界**：仅失活版本展示标记，不删历史版本行（diff 历史可溯）；不动任何业务数据；现行文件
  记录不受影响；二跑 0 失活幂等。

---

## 22. 永久备案清单（审计收敛后维持现状项）

> 本节收录经 3–9 轮审计确认**无须修复、维持现状**的设计取舍 / 理论边界 / 本地单机低危项。如未来业务或数据规模变化，再评估升级。状态：✅ 维持备案 / ⚠️ 已变化（附说明）。

| # | 备案项 | 位置 | 现状 | 维持理由 |
|---|--------|------|------|----------|
| B01 | calendar float 累加 vs snapshot Decimal 精度不对称 | `calendar.py:38-66` / `snapshot.py:117-259` | ✅ | 只读展示层，`round(2)` 后不可见，不进账本 |
| B02 | H3 不审 EUR hub 回退链（`direct is None → skip`） | `health.py:92,98` / `currency.py:101-111` | ✅ | `direct` 缺失即无可比对闭合；hub 由「宁缺勿错」纪律兜底 |
| B03 | `LLM_MODEL_CONTEXT` 定义零消费 | `config.py:62` | ✅ | 预留配置，omlx 服务端自管上下文 |
| B04 | invest 计息分母固定 365（闰年不调整） | `invest.py:224` | ✅ | 公式即口径（docstring 明示），设定精度内 |
| B05 | `_pool_balance` 跨账户求和 vs 划出仅落 primary | `invest.py:95-105` vs `:76-92,:410` | ✅ | 实测每主体每币种仅一账户 |
| B06 | demand 结息 `balance=None` 仅 recompute 当年末条回填 | `demand.py:121-126` / `leverage.py:146-152` | ✅ | 计息本体按 `inflow/outflow` 重放，不依赖 balance 列 |
| B07 | demand `ROUND_HALF_EVEN` 银行家舍入 | `demand.py:33,92` | ✅ | 对利息更公平，Decimal 默认 |
| B08 | `closed_on=12-31` 年清零 vs 日清零口径差 | `snapshot.py:53-57,120` vs `calendar.py:43-44` | ✅ | 现网 `closed_on=2002-01-01` 不触发 |
| B09 | `record_recompute_done` 健康异常仍 `done` | `recompute.py:90-102` | ✅ | 有意设计：错误入 `payload.health_error` 不静默 |
| B10 | H4 float 滚动理论漂移 / H1 UI 年份 warn 噪声 | `health.py:138-183` / `:39-45` | ✅ | 79 年复利 ×1e9 才触界；warn 非 crit |
| B11 | `overlay.create_overlay` 覆盖仍报 `idempotent=True` | `overlay.py:77-97` | ✅ | 幂等结果正确，仅返回标签宽泛 |
| B12 | `family_total_usd limit(1)` 静默取单行 | `wealth.py:52-61` | ✅ | 现行单行；多行会被唯一索引阻止 |
| B13 | `entity_merge` 同 session 读旧值风险 | `entity_merge.py:113-127` | ✅ | 低风险窗口，merge 后均 recompute |
| B14 | llm 每次新建 `httpx.Client` 资源 churn | `llm.py:21-22,29,55` | ✅ | 本地毫秒级握手，可忽略 |
| B15 | `verify_chain` 容差地板 `max(1.0,…)` / `as_of` 仅回显 | `stock_chain.py:159,188` | ✅ | 断言均为亿级金额；verify 为人工对账工具 |
| B16 | `apply_merger` 空转统计 / `cash` 无持仓静默 | `stock_cost.py:123-124,138,177` | ✅ | 无损账，仅统计口径 |
| B17 | `apply_buy unit_price=0` falsy 拒绝 / `batch_id max+1` 无约束 | `stock_cost.py:223,89-93` | ✅ | 单 worker 假设安全；0 成本股无场景 |
| B18 | `compute_interest` 不复检 `start_date` 年份 | `invest.py:217-224` | ⚠️ 已变化 | 校验已前移 `create_investment`（#93），内部仍未复检但正常路径不可达 |
| B19 | `currency_from` 裸词「克朗/法郎」回退首匹配 | `normalize.py:225-244` | ✅ | 真实标题均为复合词命中配对表 |
| B20 | `return_table` 区间标题潜伏态（封盘+% 双护栏后） | `parsers/__init__.py:236-249` | ⚠️ 已变化 | 已加封盘 + `%` 特征过滤双护栏，当前语料零触发 |
| B21 | bank 列位硬编码 / 点分日期丢行 / 节继承 | `parsers/__init__.py:786-810` | ✅ | 真实台账列序固定；源数据无点分日期 |
| B22 | 列头「万」单位不读，绝对/万单位混存 | `parsers/__init__.py:593-605` | ✅ | 现网列单位一致，混存未发生 |
| B23 | `TITLE_ENTITY` 不含 先祖/Maaike/Karel/三宝/管家 | `holders.py:24-37` | ✅ | 无银行账户，by design |
| B24 | ~~`income_security` 导入期 H2 结构性旁路~~ | `conflict.py` check_income_stream_conflict docstring | ✅ 已闭合（#211） | 旧基桩展开链路已删；basic_income 为逐年记录（year+amount+stream_type 自带），全部经导入期 H2 比对，health 兜底照旧 |
| B25 | timeline 幂等键不含 `date/note` | `writer.py:344-354` | ✅ | insert-only 设计，编辑走 overlay |
| B26 | `event_movie` 启发式 / `event_stock` 优先级 | `event_movie.py:41-65` / `event_stock.py:29-36` | ✅ | best-effort 解析器自声明 |
| B27 | `writer.fields` 只增不减 | `writer.py:42-43` | ✅ | 人物档案删字段场景未发生 |
| B28 | labor `zip` 截断 / 分隔正则 `{1,4}` / 无 update 通道 | `labor_baseline.py:118-122,85,141-147` | ✅ | 数字守卫兜底；基准变更极少 |
| B29 | `title ilike` 未转义 / `file_diff` 未套闸门 / `autoflush` 双插盲区 | `movie_events.py:67` / `versioning.py:115` / `db.py:25` | ✅ | 通配符注入仅 `%`；`file_diff` 源自 DB 非直传；盲区仅 stock 已 fix |
| B30 | stock `nonce` 客户端可换新绕过 | `stock_events.py:69` | ✅ | 本地单人语境，绕过自担 |
| B31 | transfer `nonce` 前缀碰撞仅防数字后缀（定长 hex 实践不可达） | `transfer.py:164` | ✅ | 定长 12 hex，`(?!\d)` 边界与 invest 一致 |
| B32 | `unknown_levels` 对 `None`/`""` 双重静默（此前） | `positions.py:138` | ✅ 已修复 | 现显式记 `(empty)`，见 §21.11 |
| B33 | ci.yml `PR+push` 双跑无 concurrency | `.github/workflows/ci.yml` | ✅ | 冗余非故障 |
| B34 | styles.css 注释 `":root 补"` 实为 `.viz` 作用域 | `styles.css:13` | ✅ | 措辞不符但闭环无损 |
| B35 | 测试文件名 `issue160_165_p0` 批次漂移 | `tests/test_issue160_165_p0.py` | ✅ | 已容纳多批，历史沿用 |

