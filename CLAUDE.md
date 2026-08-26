# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目性质

这不是一个软件工程代码库——没有构建、lint、测试、依赖。这是**一部中文长篇网络小说（家族·重生·金融题材）的设计与写作项目**。主角 Stijn Peeters（中文名 夏LY，1985年生），前世2070年身故而重生，手握80年全球历史记忆，1988年被收养进比利时斯帕的 Peeters 家族，1989年起以2倍极限杠杆全权操盘家族投资。

所有正文资料以 Markdown 中文文档存放在 `Design_Folder/`（PyCharm 项目名 "设计草稿"）。`.venv` 是空的（无任何依赖），`Novel_Dashboard` 仅是外层目录名。

因此本项目没有可运行/可测试的命令；唯一相关的"命令"是下方 webnovel-writer 插件的斜杠技能（通过 Claude Code 调用）。

## 工作流命令（webnovel-writer 插件）

以 `/` 前缀在 Claude Code 中调用（完整插件名是 `webnovel-writer:*`，日常用短名即可）：

- `/webnovel-init`：深度初始化项目骨架（当前项目**未初始化**，缺少 `.webnovel/state.json`；首次接手建议先跑这个建立 canonical 与约束文件）
- `/webnovel-plan`：基于总纲生成卷纲、时间线、章纲，并把新设定写回设定集
- `/webnovel-write`：产出可发布章节（上下文→起草→审查→润色→提交→备份）
- `/webnovel-review`：用审查 Agent 评估章节质量
- `/webnovel-query`：查询设定、角色、力量/资产体系、势力、伏笔
- `/webnovel-doctor`：只读体检目录/文件/JSON/RAG 配置完整性
- `/webnovel-dashboard`：只读管理面板（项目状态、实体图谱、章节内容）

另有 `/story-*` 系列通用网文工具箱（扫榜/拆书/去AI味/封面等），与具体书目无关。

## Design_Folder 结构与语义

`Design_Folder/` 是本文资料的唯一权威出处（canon）。关键点：**`时间线.md` 是整个项目的锚定索引**，其余子目录是它的细节来源；任何一个数据文件的增删改都必须回写 `时间线.md` 保持一致。

| 目录/文件 | 内容 |
|-----------|------|
| `时间线.md` | **主时间轴锚点**。按 decade 记录家族关键事件、投资、资产汇总；头部含"账户流水规则"总表 |
| `人物/` | 角色档案。`主角.md`、`Peeters 家族/`（祖父母、养父母、Maaike&Karel 双胞胎、管家人事）、`Stijn小家庭/`（三宝） |
| `经济/` | `股票/`（模版+个股台账）、`银行/`（`个人/`、`公司/`，均有模版） |
| `基准/` | 数理底稿，用于给小说数值"算得出来"：`收益表/`（各国 1983/1947—2025 R1–R5 风险分级年化收益测算：比利时、卢森堡、荷兰、丹麦、瑞典、美国、英国、中国香港、中国大陆）、`汇率/`（逐年）、`薪资/`、`初始资产/`、`公司/用工成本/`（分国）、`事件/`、`CPI工资.md`、`1974-2001家庭支出.md` |
| `设计文件/` | 创作约束笔记，如 `学习.md`（三兄妹 KU Leuven 学业轨——药学 / 古典语文学 / 神学+教会法） |

### 不可改的数值纪律（沿用项目的明确规则）

1. **币种归属固定**，各账户只用自己的币种核算：祖父=BEF+LUF（比利时+卢森堡祖产）、祖母=SEK、外祖父=NLG、外祖母=DKK、养父=BEF（薪资）、养母=BEF（薪资+投资管理人）。现金注入公司后按注册地换算（USD、HKD 归 Peeters Americas / Peeters Asia）。
2. **同一币种流水放一起，不同币种分开核算**；流水不做时间切片，只留**最后汇总**。
3. 收益率以 **R1–R5 风险分级** 体系锚定（R1 保本 → R5 高杠杆商品/外汇/期权），家族主仓按 `2倍杠杆` 上限（1989年前1.5倍），遇历史事件（1992黑色星期三、2008危机、2016脱欧、2022特拉斯迷你预算）有重生记忆套利。
4. **数据要算得平**：任何金额改动引发的复利/汇率/杠杆传导，都要能回推自洽；改了一个数字要顺着 `时间线.md` 的汇总链核对下游。
5. **学业/时间一致性**：角色求学轨迹（如 KU Leuven 基准入学 1996.9）与 `时间线.md` 的年表必须吻合，防止"悬浮"设定。
6. **银行台账复利 opt-in**（issue #113 定案 A）：默认不对源台账套曲线复利（文件即权威）；
   仅 `entity.fields["compound"]=true` 的账户参与 §7.2 滚动。地区/R 覆盖：`return_region`/`risk_lvl`。
7. **收益展开因子 A 口径「文件终值权威」**（issue #114 定案）：调价在每年年初（含起租年 1974）、
   年末结算入账——factor(1984)=1.07¹¹≈2.1049、factor(2007)≈5.2100 与收益文件示例一致；
   income(1974)=基桩×1.07。改因子后用 `ingest --force` 重浇灌。

历史主线投资（作为共识背景）：皮克斯→迪士尼换股、阿里巴巴、腾讯（港）、联合技术/GE 系拆分股（GE/GEHC/GEV/UTX→CARR/OTIS/RTX）、漫威、影视投资（泰坦尼克号、指环王、阿凡达、霍比特人、侏罗纪世界、阿丽塔）。

## 达成一致的行为约定

- 回复与文档用**中文**（本项目全部内容是中文）。
- **先读再改**：改任何人物/资产/账户/事件文件前，先读对应文件与 `时间线.md`，遵循其既有的表格格式与数值口径。
- **改后必同步**：新增/修改档案或数据后，更新 `时间线.md` 的对应 decade 条目与"最后更新"日期，保持事件链完整。
- 不确定某一设定是否已有 canon 时，先 `/webnovel-query` 查证，不要凭记忆新造设定。

---

## 项目是双层：写作线 + Dashboard 工程线

本仓库同时承载**两条线**，上方 `项目性质`/`Design_Folder 结构` 描述的是**写作线**；另有 **Dashboard 数据工程线**（开发中）：

- **写作线**：`Design_Folder/` 创作素材（走 webnovel-writer 插件，数值纪律见上，沿用原规则）。
- **Dashboard 工程线**：一个**本地网文创作数据 Dashboard**（Postgres + FastAPI + ingest(Python) + React+Vite），把创作草稿解析→可视化，并提供界面更新数据能力（源 md 只读）。

**权威文档（开发以此为准，先读再改）**：
- `docs/PRD-webnovel-dashboard.md` —— 需求（分阶段导入、收益挂账、四类 UI 改数据操作、资产模型）
- `docs/DESIGN-webnovel-dashboard.md` —— 技术设计（DDL、ingest、增量重算、健康校验、LLM 搜索、**开发功能清单 §20**）
- `docs/ui-mockup/index.html` —— 10 屏 UI 原型

**Dash价值前已锁定的核心约束（详见 DESIGN，此处仅指针）**：
- 源 md（`Design_Folder/`）只读、绝不回写；数据更新走文件导入 + 少量 UI 派生。
- 账务本金记账、展示层才折算 USD；BEF/LUF/NLG 2002 关池转 EUR；收益文件模块化挂账。
- 开发进度以 `docs/DESIGN-webnovel-dashboard.md` §20 功能清单（编号 `F-P0-xx`/`F-P1-xx`/`F-P2-xx`，状态图例 ⬜/🟨/✅）为准，完成一项勾一项，在需求/任务/commit 中以此编号引用。

**启动 / 运行 Dashboard（F-P0-01..14，Phase 1 P0 已实现）**：

前置（一次性）：
1. **Postgres**：本机 `Postgres.app`（PostgreSQL 18），端口 `5432`；需已有三库 `novel_dev` / `novel_test` / `novel_prod`（同名库已建，缺失可用 `createdb` 补）。免密 trust 即可；若设了密码，导出 `export POSTGRES_PASSKEY=...`。
2. **Python 依赖**：`cd 项目根 && .venv/bin/pip install -r requirements.txt`。
3. **迁移（三库各建表）**：
   ```bash
   APP_ENV=dev  .venv/bin/alembic upgrade head   # test/prod 同理
   ```
4. **摄入真实数据（从 Design_Folder）**：
   ```bash
   APP_ENV=dev .venv/bin/python -m app.ingest.main ingest
   APP_ENV=dev .venv/bin/python -m app.ingest.main recompute --from 1947
   APP_ENV=dev .venv/bin/python -m app.ingest.main snapshot
   ```
   > 环境（issue #107 修复后）：`--env` 缺省回落 `APP_ENV`，再缺省 dev；回显一律以命令输出
   > `[<env>]` 为准。跨环境操作推荐显式 `--env test|prod`。

启动服务：
- **后端 API**（FastAPI，Swagger 在 `/docs`）：
  ```bash
  APP_ENV=dev .venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8001
  ```
  > 注：`8000` 常被本地其他程序占用，实测用 `8001` 更稳。
- **前端**（Vite dev server，代理 `/api` → 后端）：
  ```bash
  cd frontend && npm install   # 首次
  npm run dev                   # 默认 http://localhost:5173
  ```

CLI 全清单（14 个子命令，均支持 `--env dev/test/prod`，缺省回落 `APP_ENV`）：
```bash
.venv/bin/python -m app.ingest.main ping                    # 连通自检（打印实际 env/DSN）
.venv/bin/python -m app.ingest.main run                     # 扫描+解析报告（不落库）
.venv/bin/python -m app.ingest.main ingest [--force]        # 落库主链路；--force 重浇灌四类收益文件(#114/#135 已修复)
.venv/bin/python -m app.ingest.main health                  # H1-H5/H-STOCK 健康校验
.venv/bin/python -m app.ingest.main recompute --from 1947   # 增量重算（杠杆复利仅 compound opt-in 账户）
.venv/bin/python -m app.ingest.main snapshot --from 1947    # 重建逐年快照
.venv/bin/python -m app.ingest.main wealth --year 2001      # 家族合计(USD)+分币种
.venv/bin/python -m app.ingest.main calendar --as-of 2001-12-30
.venv/bin/python -m app.ingest.main labor-baseline --office be   # 用工基准三表导入（office 支持中文或 ISO 缩写 be/lu/nl/dk/se/uk，#144）
.venv/bin/python -m app.ingest.main search-index            # 搜索索引构建（pgvector）
.venv/bin/python -m app.ingest.main finance-backfill        # finance_entry 存量回填
.venv/bin/python -m app.ingest.main events-movie            # Phase2 电影事件导入
.venv/bin/python -m app.ingest.main events-stock            # Phase2 股票事件导入
.venv/bin/python -m app.ingest.main merge-alias-persons [--dry-run]  # 职称别名 person 并入规范实体（#136 存量修复，幂等）
```
三环境均同源代码，仅 `APP_ENV` + 库名(`novel_*`) + 数据目录不同；`Design_Folder` 为只读源。

**仓库与 git**：
- **入库范围**（Dashboard 工程线全部代码入版本库）：`app/`（FastAPI + ingest + 模型）、`frontend/`（React+Vite）、`migrations/` + `alembic.ini`、`tests/`、`requirements.txt`、`docs/`、`CLAUDE.md`、`.gitignore`、`data/*`（仅 `.gitkeep` 占位）。
- `Design_Folder/`（创作素材）已被 `.gitignore` 排除，**不入 git、不提交**。
- 远程：`origin` → github.com/LinyunXIA/Novel_Dashboard（private）。