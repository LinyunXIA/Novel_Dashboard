# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目性质

本地**网文创作数据 Dashboard**：以**展示与计算 `Design_Folder/` 下静态 Markdown 文件内容**为主的程序。它把创作草稿（异构中文 `.md`：`时间线.md`、`人物/`、`经济/银行/`、`基准/收益表/` 等）解析 → 归一化入本地 Postgres → 可视化（财富曲线 / 人物·公司图谱 / 检索 / 健康校验），并提供有限界面更新能力。

- **数据底座**：`Design_Folder/` 是唯一权威数据出处；源 md **只读、绝不回写**。数据更新走「数据调整员文件导入（prod 按激活清单 `import_files.yaml` 门控）+ 少量 UI 派生」。
- **技术栈**：Postgres(`novel_dev/test/prod` 三独立库) + FastAPI + ingest(Python) + React/Vite。
- 工程线代码入 git；`Design_Folder/`（创作素材）已被 `.gitignore` 排除，不入 git。
- 回复与文档用**中文**。

> 早期定位为「小说写作 + webnovel-writer 写作插件」双线；现已收敛为上述展示/计算侧主线，写作插件相关命令已移除。

## 权威文档（开发以此为准，先读再改）

- `docs/PRD-webnovel-dashboard.md` —— 需求（分阶段导入、收益挂账、四类 UI 改数据操作、资产模型、里程碑）
- `docs/DESIGN-webnovel-dashboard.md` —— 技术设计（DDL、ingest、增量重算、健康校验、LLM 搜索、**开发功能清单 §20**）
- `docs/ui-mockup/index.html` —— UI 原型

**核心约束（详见 DESIGN，此处仅指针）**：
- 源 md 只读、绝不回写；数据更新走文件导入 + 少量 UI 派生。
- 账务本金记账、展示层才折算 USD；BEF/LUF/NLG 2002 关池转 EUR；收益文件模块化挂账。
- 开发进度以 `docs/DESIGN…` §20 功能清单（`F-P0/P1/P2/P2+/P3-*`，状态 ⬜/🟨/✅）为准，在需求/任务/commit 以此编号引用。

## Design_Folder 结构与语义（数据模型）

`Design_Folder/` 是唯一权威（canon）；**`时间线.md` 是锚定索引**，其余子目录是细节来源；任何数据增删改都需回写 `时间线.md`。

| 目录 | 内容 |
|------|------|
| `时间线.md` | 主时间轴锚点，按 decade 记家族关键事件/投资/资产汇总 |
| `人物/` | 角色档案（`主角.md`、`Peeters 家族/`、`Stijn小家庭/`） |
| `经济/` | `股票/`、`银行/`（个人/公司，含模版） |
| `基准/` | 数理底稿：`收益表/`(各国 R1–R5 年化)、`汇率/`、`薪资/`、`初始资产/`、`公司/用工成本/`、`事件/`、`CPI工资.md`、`1974-2001家庭支出.md` |
| `设计文件/` | 创作约束笔记（如 `学习.md` 三子女学业轨） |

**不可改的数值纪律**（沿用明确规则）：
1. 币种归属固定、各自核算；不同币种分开，流水只留最后汇总。
2. 收益率锚定 R1–R5，家族主仓 2 倍杠杆上限（1989 前 1.5 倍）。
3. **数据要算得平**：改动导致的复利/汇率/杠杆传导须能回推自洽，改后沿 `时间线.md` 汇总链核对下游。
4. 学业/时间一致性：角色求学轨迹与 `时间线.md` 年表必须吻合。
5. 银行台账复利 opt-in（issue #113）：仅 `entity.fields["compound"]=true` 参与滚动；`return_region`/`risk_lvl` 覆盖。
6. 收益展开因子 A「文件终值权威」（issue #114）：年初调价、年末入账；改因子用 `ingest --force` 重浇灌。

历史主线投资（共识背景）：皮克斯→迪士尼换股、阿里巴巴、腾讯(港)、GE/UTX 系拆分股、漫威、影视投资（泰坦尼克/指环王/阿凡达…）。

## 达成一致的行为约定

- **先读再改**：改任何数据/档案前，先读对应文件与 `时间线.md`，遵循既有表格格式与数值口径。
- **改后必同步**：新增/修改后回写 `时间线.md` 对应 decade 与「最后更新」。
- 不确定设定是否已有 canon 时先查证，不凭记忆新造。

## 运行 / 运维（命令集中在独立文件）

- **一键起服务**：`Design_Folder/start_dashboard.sh`（APP_ENV=prod，后端 8001 + 前端 5173）。
- **完整命令**（前置/迁移/摄入、启动服务、CLI 15 子命令、数据整理员工作流 `import_files.yaml`/`reset`）→ **[docs/运行指南.md](docs/运行指南.md)**。
- 开发进度皆以 DESIGN §20 为准。

## 仓库与 git

- **入库范围**：`app/`、`frontend/`、`migrations/`+`alembic.ini`、`tests/`、`requirements.txt`、`docs/`、`CLAUDE.md`、`.gitignore`、`data/*`（仅 `.gitkeep`）。
- `Design_Folder/` 已被 `.gitignore` 排除，不入 git、不提交。
- 远程：`origin` → github.com/LinyunXIA/Novel_Dashboard（private）。