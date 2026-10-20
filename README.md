# LiveOps Harness

一个可本地运行的直播运营 Agent 项目。它使用 LangGraph 组织指令处理流程，用持久化 Checkpoint 暂停和恢复高风险操作，用 SQLite 模拟直播间业务状态，并在数据库事务中完成幂等写入与审计。默认离线运行，无需淘宝账号或模型 API Key。

新增 **LiveOps Multi-Agent Harness** 商品准备链：选品、手卡、合规、汇总四种角色通过只读 DAG 协作，支持同层并行、角色隔离、材料外置、任务断点和失败分支恢复。与原审批写入链分开，不自动执行材料中的改价或发券建议。详见 [升级设计、文章对照和面试说明](HARNESS_UPGRADE.md)。

面试重点、真实自检缺陷、解决方案和替代方案取舍，见 [亮点难点与方案取舍](HIGHLIGHTS_AND_TRADEOFFS.md)。

2026-10-17 补充：[多角色受控实验](/Users/wumao/Downloads/项目报告汇总/liveops-harness/MULTI_AGENT_EXPERIMENTS_20261017.md) 与 [业务落地、成本和延迟缺陷审查](/Users/wumao/Downloads/项目报告汇总/liveops-harness/ENGINEERING_GAPS_AND_BUSINESS_PLAN.md)。新增失败模型调用的用量留存与只读指标接口；仍缺生产鉴权、全局费用预算与真实平台验证，仅适合本地沙箱。

2026-10-18 新增 [真实DeepSeek实验结果](/Users/wumao/Downloads/项目报告汇总/liveops-harness/REAL_MODEL_RESULTS_20261018.md)：24条意图开发题、串并行各5次真实模型准备、54次正式请求的供应商usage，以及8类本地记忆契约。保留4个意图失败，不代表线上泛化效果。

2026-10-19 新增 [对话能力与可操作 Harness 工作台](CONVERSATION_UPGRADE_20261019.md)：DeepSeek 自然语言建议与口播、近期四轮上下文、推荐顺序与指代绑定、缺参澄清、带来源的草稿、网页备播阶段推进/恢复、偏好删除与远端回执对账。自动化回归增至125项；最终10条真实开发对话的9项流程检查通过。调试中观察到生成价格被检查拦截后的事实回退，流程通过不代表回答质量100%正确。

## 能做什么

2026-10-19 本次补强见 [文章架构落地与真实验证](ARTICLE_HARNESS_20261019.md)：真实阶段 Reducer、分场景上下文与原文外置、跨阶段审批失效、持久化模型预算、单品增量重规划和建议反馈指标。**160项回归通过**；真实DeepSeek换品对照中恢复调用3→1、任务8→4，耗时接近，不宣称显著提速。仍是本地业务沙箱，不是淘宝线上部署。

降低学习成本，先读 [Claude规划对照与易记升级路线](CLAUDE_PLAN_SIMPLIFIED_20261019.md)：区分已有机制、下一步建议和暂不需要的复杂度，附业务例子与30秒面试回答。

2026-10-19 第一阶段工程闭环见 [工具契约与生命周期检查](CONTRACTS_HOOKS_20261019.md)：12个指令动作与4个准备技能统一登记契约，恢复策略集中为一张决策表，Trace 改为记录实际执行的检查。**195项回归通过（原160项＋新增35项）**；本阶段没有调用真实模型，也没有新的效果数据。

2026-10-19 第二至四阶段见 [指代绑定、任务规格、场景评测与隔离消融](REFERENCE_SPEC_EVAL_20261019.md)：“第二款”绑定到列表ID、来源指令和计划版本，换预算或重规划后旧序号与旧审批在提交事务内失效；选品和备播先生成可校验的任务规格再选固定模板；反馈新增封闭原因码。52条手写场景题离线全通过（其中1个缺陷是在留出集上发现并修复的，留出集不算盲测）；隔离消融中去掉原话核对出现6次违规执行、去掉提交时复核出现3次。**215项回归通过**；没有调用真实模型。

2026-10-20 [真实模型实验](REAL_MODEL_EXPERIMENTS_20261020.md)：同一套 52 题在 DeepSeek 上跑了 5 轮，违规执行 0、重复写入 0，最终一轮 52/52（手写题，不是盲测）。6 组 × 60 轮长对话中，全量历史用的 token 约是“最近 4 轮加结构化状态”的 2.1 倍，“第二款”和“它”却全部答不对；后者经代码核对后，序号、焦点、预算都是 24/24。实验还发现“某款先不上”在所有方案里都会丢，已改为本场排除名单，由代码执行（修复后 24/24，模型自己仍为 0/24）。面试材料见 [INTERVIEW_GUIDE_20261020.md](INTERVIEW_GUIDE_20261020.md)。

同日下午：由一个没读过源码的子 Agent 写了 72 题独立盲测集，并且在跑之前就提交进 git。真实模型首跑 3 次分别是 63、64、63，违规执行和重复写入都是 0（只有这一次算盲测成绩）。根据失败做了几项修复：解析和核对共用写操作词表，用户没说条数时不让模型自己定，新增规则快路径（`LIVEOPS_INTENT_ROUTING=hybrid`，默认开启）。在同一套题上，快路径和“全部走模型”的准确率持平，模型请求少 15.8%，token 少 15.1%。修复后确认运行 70/72。项目现在是 git 仓库，依赖版本锁在 `requirements.lock`。**228 项回归通过。**

- 播前：查询商品、制定选品顺序、生成基于真实商品字段的讲解手卡。
- 播中：切换讲解商品、切回上一款、申请改价、申请发券、模拟库存变化。
- 播后：根据执行日志复盘，保存主播明确提出的偏好。
- Harness：工具白名单、风险分级、限时人工审批、断点恢复、状态版本检查、事务幂等、分型错误反馈与审计回放。

## 一分钟运行

需要 Python 3.11 或更新版本：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock   # 复现实验时使用的精确版本
.venv/bin/python -m pip install -e '.[dev]' --no-deps
.venv/bin/python -m uvicorn liveops.api:app --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765>。接口文档位于 <http://127.0.0.1:8765/docs>。

默认创建 `LIVE-DEMO-001` 和 5 件护肤商品。数据保存在 `data/liveops.db`，LangGraph Checkpoint 保存在 `data/checkpoints.db`。设置 `LIVEOPS_DB_PATH` 和 `LIVEOPS_CHECKPOINT_PATH` 可改路径。

## 演示路径

1. 输入“给我做一个选品计划”和“介绍当前商品”，观察计划与手卡。
2. 输入“切到 SK-102”，观察当前商品和审计记录更新。
3. 输入“把面霜价格改成 119 元”，在右侧确认或拒绝审批。
4. 输入“给面霜发 10 元券 20 张”，先不要审批；在“故障演示”中将面霜库存改为 0，再点击确认。旧状态上的动作会被拦截。
5. 再提交一条待审批操作，重启服务，待审批操作仍可继续处理。
6. 调用同一个 `command_id` 重发 HTTP 请求，返回原结果，不重复提交业务动作。
7. 输入“切回上一款”；操作会读取持久化的前一商品，而不会猜测聊天记录。

## 可选模型解析

未设置环境变量时使用离线解析器，覆盖演示中的常见中文指令。可配置兼容 OpenAI Chat Completions 的模型：

```text
LIVEOPS_LLM_BASE_URL=https://your-compatible-endpoint/v1
LIVEOPS_LLM_API_KEY=your-key
LIVEOPS_LLM_MODEL=your-model
```

模型只提出结构化动作；商品存在性、风险、价格下限、审批和事务提交均由确定性代码执行。模型遇到 429/超时等暂时性失败最多调用两次，坏 JSON 最多要求修正一次，随后记录降级原因并退回离线解析；4xx 参数错误不会盲目重试。模型上下文只取相关商品、当前/上一款和明确保存的偏好，不把旧摘要当作实时库存。审批默认 300 秒过期，可通过 `LIVEOPS_APPROVAL_TTL_SECONDS` 配置。环境变量需在启动前自行导出；`.env.example` 不会自动加载。不要将 `.env` 提交到仓库。

### 本机私有DeepSeek配置

当前机器已将凭据保存在项目外的 `~/.config/liveops-harness/deepseek.env`，目录700、文件600，不随源码提交。加载是显式的，默认离线模式不变：

```bash
.venv/bin/python -m liveops.private_config -m uvicorn liveops.api:app --host 127.0.0.1 --port 8765
```

不要在旧服务占用端口时重复启动。私有配置设置 `deepseek-flash` 和关闭思考，模型解析单次输出上限500 token；thinking是可选供应商字段，未配置时不会发送。真实评测脚本会产生API消耗，不包含在pytest自动运行中。

## 测试

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m liveops.benchmark --requests 100 --workers 1 4 8
.venv/bin/python -m liveops.remote_demo
.venv/bin/python -m liveops.campaign_demo
.venv/bin/python -m liveops.operations_eval
.venv/bin/python -m liveops.scenario_eval        # 52条场景题×5个隔离消融变体，离线，写 outputs/scenario_eval_20261019.json
.venv/bin/python -m liveops.scenario_eval --real-provider --max-requests 200 --output outputs/x.json   # 会产生API消耗
.venv/bin/python -m liveops.long_dialogue_eval --real-provider --output outputs/y.json                 # 288次请求，会产生API消耗
```

测试覆盖审批拒绝与过期、低于成本价拦截、同一请求幂等、切品与跨重启的“上一款”记忆、库存变化导致旧操作失效，以及本地页面可用性。

此外还覆盖竞争改价、发券上限、请求 ID 冲突、模型动作白名单、模型失败降级、多指令并发和下述远端协议仿真。原单指令链有 33 项测试，准备链与记忆有 38 项，离线实验脚本有 2 项，用量与指标有 10 项，私有配置与真实评测工具的离线测试有 6 项，合计 89 项。压测脚本只测本机离线只读指令链路，并包含命令、审计和 Checkpoint 的 SQLite 写入；结果不是线上容量承诺。

2026-10-19 另增36项对话、生成契约与页面接口回归，总计125项。真实开发样例可显式运行 `.venv/bin/python -m liveops.conversation_eval`，会加载本机私有配置并产生API消耗，最多32次请求；不要当成独立泛化评测。

## 发券响应丢失：协议仿真初版

`liveops.remote_demo` 会通过 HTTP 测试客户端执行审批、注入提交成功后的响应丢失、重建应用并查询回执。演示轨迹保存到 `outputs/remote_recovery_demo.json`：平台只提交一次，本地在确认回执后才写入优惠券和幂等账本。

服务模式启用方式：

```bash
LIVEOPS_COUPON_BACKEND=simulated_remote .venv/bin/python -m uvicorn liveops.api:app --host 127.0.0.1 --port 8765
```

只将发券切到独立 SQLite 仿真平台，其余工具沿用本地实现。接口文档 `/docs` 可调用 `POST /api/simulator/remote-fault` 注入 normal/lost_response/delayed_visibility/before_commit/query_unavailable/reject；正常指令与审批接口保持不变。操作状态通过 `GET /api/operations/LIVE-DEMO-001` 查看，`POST /api/commands/{command_id}/reconcile` 查询并对账。

未知状态不会自动重发；连续三次仍未确认转 manual_review。已发送操作在审批过期后仍需查询实际结果，尚未发送操作则重新检查有效期与版本。记录绑定不可变动作 hash，回执的 ID、SKU、金额和数量必须匹配才能写入本地。当前是同步推进、显式查询的 outbox 初版，没有后台调度、真实角色鉴权或淘宝接口。

## 技术取舍

当前项目是**淘宝直播业务场景仿真**，没有调用淘宝生产接口，也没有真实改价或发券。Medusa 可作为后续商品、库存、促销模块的业务后端；本地版先用 SQLite 业务适配器保证开箱即用。Langfuse、LiveKit 和 OPA 分别适合后续增加可观测、实时语音和复杂策略，但不是初版运行的前置依赖。

## 简历可用表述

**LiveOps Harness｜直播运营 Agent 安全执行系统（独立项目）**

- 基于 LangGraph + FastAPI 实现播前选品、播中切品/改价/发券、播后复盘的可运行运营沙箱；模型仅输出结构化候选动作，业务状态与商品事实由 SQLite 适配器提供。
- 设计风险分级与限时人工审批流程：改价和发券经持久化 Checkpoint 暂停，重启后恢复；提交前用状态版本拦截旧状态操作，使用事务幂等键防止重复写入。
- 将模型解析失败分型并有限降级，以同指令互斥、不同指令并行替换全局串行锁；实现独立账本的发券协议仿真、持久化 outbox 与 unknown 对账流程，使用自动化测试及离线演示检查边界。

这些是本地实现与测试的能力描述，不代表已接入淘宝或取得真实成交提升。

## 多角色准备链接口

- `POST /api/campaigns`：准备目标与最多 5 个 SKU，创建有界计划；支持固定 `run_id` 重放。
- `POST /api/campaigns/{live_id}/{run_id}/advance`：推进计划，支持 `max_waves` 暂停和 `retry_failed` 有界恢复。
- `GET /api/campaigns/{live_id}/{run_id}`：读取计划、逐任务状态和 Trace。
- `GET /api/campaigns/{live_id}/{run_id}/metrics`：汇总已提交 Trace 的模型调用、失败消耗、已知 token 与用量缺失；不提供真实货币账单，也不是崩溃安全的请求前预算账本。
- `GET /api/campaigns/{live_id}/{run_id}/artifacts/{artifact_id}`：回查完整材料。
- `GET /api/skills`：角色级只读技能许可。
- `GET /api/memory/{live_id}?scene=pre_live`：读取三层来源记忆视图。
- `DELETE /api/memory/{live_id}/preference`：显式删除当前偏好，不删除审计与检查点。

默认 Planner 与角色为规则/模板；设置 `LIVEOPS_SPECIALIST_MODEL` 后，手卡角色使用独立模型调用，沿用模型服务地址与密钥。合规核对与业务安全门禁仍由代码执行，不是任意任务的自主多智能体系统。

详见 [ARCHITECTURE.md](ARCHITECTURE.md)、[EVALUATION.md](EVALUATION.md)、[INTERVIEW_GUIDE.md](INTERVIEW_GUIDE.md) 和 [HARNESS_UPGRADE.md](HARNESS_UPGRADE.md)。
