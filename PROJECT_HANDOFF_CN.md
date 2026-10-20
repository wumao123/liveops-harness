# LiveOps Harness 项目交接（新会话接续用）

> **2026-10-20 更新**：已完成真实模型场景评测（5 轮）和 60 轮长对话实验，新增本场排除约束，回归 **220 passed**，详见 `REAL_MODEL_EXPERIMENTS_20261020.md`；面试材料见 `INTERVIEW_GUIDE_20261020.md`。剩余：真实口语说法的约束识别评测、其他约束槽位、生产缺口（第 5 节 P3）。8765 服务仍未重启。
>
> **2026-10-19 深夜更新**：第二、三、四阶段和反馈原因已完成，回归 **215 passed**，详见 `REFERENCE_SPEC_EVAL_20261019.md`。下文第 0～5 节中“下一步是阶段二”“195 项”等描述是更新前的状态，以该文档和第 3 节补记为准。
>
> 整理时间：2026-10-19 晚。依据当前磁盘上的源码、文档、`outputs/` 实验记录和 `data/` 日志，并于本次整理前运行全量测试（195 项通过）。
> 无法从文件确认的内容标注“待核实”。本文不包含任何密钥或 token。
> 本次只整理文档，没有修改实验逻辑、没有调用真实模型，也没有重启服务。
> 说明：本项目没有模型训练环节；文中“checkpoint”指 LangGraph 审批断点和备播计划的任务断点。

---

## 0. 一页速览

| 项 | 内容 |
|---|---|
| 项目 | 直播运营（主播助理）Agent Harness 本地仿真：播前准备手卡、播中查询/切品/改价/发券、播后复盘 |
| 用途 | Agent/Harness 工程岗位的简历与面试项目，对标文章《更可靠的主播助理：淘宝主播Agent的Harness工程实战》 |
| 技术栈 | Python 3.11、LangGraph 1.2.12、langgraph-checkpoint-sqlite 3.1.1、FastAPI 0.141.1、Pydantic 2.13.5、HTTPX 0.28.1、SQLite、DeepSeek（OpenAI 兼容接口） |
| 当前回归 | `.venv/bin/python -m pytest -q` → **195 passed**（2026-10-19 晚实测） |
| 最后完成 | 第一阶段“工具契约、统一恢复决策、Hook、真实检查记录”，见 `CONTRACTS_HOOKS_20261019.md` |
| 下一步 | 第二阶段“指代来源绑定与任务规格”，还没有开始写代码 |
| 运行中进程 | 本机 `127.0.0.1:8765` 有一个 uvicorn 服务（PID 4827，下午 3:30 启动），启动早于第一阶段代码，**运行的是旧代码**，详见第 3 节 |
| 生产边界 | 没有接淘宝接口、没有真实交易、没有鉴权与多租户，只能在本机演示 |

---

## 1. 最初规划

### 1.1 目标

把一个“LangGraph 单指令审批链”的直播运营沙箱，升级成能讲清架构取舍的 Agent Harness 项目，参照文章的 H=(E,T,C,S,L,V) 六维结构：执行循环、工具契约、上下文、状态、生命周期 Hook、评测。约束有两条：

- 全部在本地可运行、可复现；
- 文档只写实测数字，Mock、仿真、真实模型三类结果分开标注，不引用文章里的数字（0.847、0.737 等）。

用户画像的业务定位（出自 `ENGINEERING_GAPS_AND_BUSINESS_PLAN.md`）：面向小型直播团队的播前手卡准备、播中商品查询和优惠操作提案助手。不定位成全自动经营或提升 GMV。

### 1.2 研究问题

1. 模型提出的写操作，怎样保证被检查、被审批、能恢复、不重复执行？
2. 多角色并行准备时，怎样防止上下文串用、材料过期，并且失败后只重做受影响的分支？
3. 多轮对话里，“第二款”“它”怎样绑定到正确的商品，同时不把旧价格当作实时事实？
4. 重试、修复和重规划会不会让调用量成倍增加？怎样记账？
5. 怎样证明以上机制有效，而不是“看起来更智能”？

### 1.3 技术路线（按时间）

| 时间 | 阶段 | 主要文档 |
|---|---|---|
| 2026-04～08（简历时间段）/ 9 月底建仓 | 单指令链：LangGraph 解析→风险门→审批中断→事务提交；幂等账本、状态版本、远端发券 outbox 与 unknown 对账 | `ARCHITECTURE.md`、`EVALUATION.md` |
| 10-16 | 多角色只读备播 DAG：选品→手卡×N→审核×N→汇总；租约、原子提交、分支恢复、三层记忆视图 | `HARNESS_UPGRADE.md`、`HIGHLIGHTS_AND_TRADEOFFS.md` |
| 10-17 | 受控实验、失败 usage 留存、成本与延迟审查 | `MULTI_AGENT_EXPERIMENTS_20261017.md`、`ENGINEERING_GAPS_AND_BUSINESS_PLAN.md` |
| 10-18 | 真实 DeepSeek 实验：24 条意图题、串/并行对照 | `REAL_MODEL_RESULTS_20261018.md` |
| 10-19 上午 | 对话层：只读建议与口播、近 4 轮上下文、指代、缺参澄清、网页工作台 | `CONVERSATION_UPGRADE_20261019.md` |
| 10-19 下午 | 对照文章落地：阶段 Reducer、分阶段上下文、跨阶段审批失效、持久化模型预算、增量重规划、反馈指标 | `ARTICLE_HARNESS_20261019.md` |
| 10-19 晚 | 第一阶段：工具契约、恢复决策表、Hook、真实检查记录、SKU 来源校验 | `CONTRACTS_HOOKS_20261019.md` |

### 1.4 用户最终确认的后续范围（以此为准）

第一版规划由 Claude 提出：六步升级，包括 LLM PlanEngine 对比 ReAct、信任分自进化等。用户随后收窄了范围，取舍说明见 `CLAUDE_PLAN_SIMPLIFIED_20261019.md`：

| 阶段 | 内容 | 约束 |
|---|---|---|
| 一（已完成） | 轻量工具契约目录；少量真实触发的 Hook；Trace 只记录实际执行过的检查；统一“是否重试”的决策 | 复用现有校验，不做插件平台。JSON 只修结构，不改金额、数量或 SKU。远端写入超时必须保留 unknown 并对账。关键检查不能被可选 Hook 绕过 |
| 二 | “第二款”同时绑定列表ID、来源轮次和计划版本；换预算或换方案时说明哪些内容失效；先做结构化任务规格，再判断要不要 LLM Planner | 不直接上自由生成 DAG |
| 三 | 评测先于新功能：以现有模板为基线，样本覆盖简单、复合、歧义、需求变更、故障；分别统计正确完成、正确拒绝、误拦截、违规执行、重复写入、调用量和延迟 | 合法拒绝也算正确 |
| 四 | 安全消融 | 只在隔离的临时库和仿真平台中进行，不能给运行中的服务增加“关闭审批”的入口 |
| 暂缓 | 信任分自适应（先补反馈原因和替代方案）、GitLab Skill 管理、向量库、训练 Planner、Redis/Kafka/MySQL | 采纳不等于正确，信任分不能用来放宽交易权限 |

### 1.5 预期实验与验收标准（规划值，未全部完成）

出自 `CLAUDE_PLAN_SIMPLIFIED_20261019.md` 第五节和 `EVALUATION.md`“后续离线评测计划”：

| 实验 | 建议样本 | 对照 | 指标 |
|---|---|---|---|
| 意图与约束理解 | 40 条：简单、复合、歧义、变更各 10 条 | 规则、当前模型流程，可选混合路由 | 关键字段匹配、约束保留、该澄清时是否澄清 |
| 安全与故障 | 40 条：正常 20 条，攻击和异常 20 条 | 完整防线，以及隔离环境中的逐层消融 | 违规执行、误拦截、首个拦截层、副作用 |
| 长对话 | 6 组 × 60 轮 | 全量历史、最近 4 轮、最近 4 轮加结构化状态与原文索引 | 焦点、预算、否定约束和列表来源是否保留 |

要求：开发集和留出集分开；同一会话的改写不能拆到两边；带随机性的方案重复多次；预算固定，按题报告调用数、usage 缺失和延迟分布。

---

## 2. 已完成工作

### 2.1 代码模块（`liveops/`，以磁盘当前版本为准）

| 文件 | 作用 |
|---|---|
| `agent.py` | LangGraph 单指令链 load→interpret→gate→execute→respond。包含规则解析 `_rule_action`、模型解析 `_llm_action`、原话核对 `_guard_instruction`（金额、张数、SKU 来源、条件句不执行）、契约风险下限、审批 interrupt、`_execute`（Hook 包裹）和 `_execute_tool`（实际工具） |
| `contracts.py` | **第一阶段新增**。12 个指令动作和 4 个准备技能的契约：角色、读写类型、风险下限、超时、幂等方式、未知结果处理、输入/输出 Schema。`check_catalog` 在注册时自检 |
| `hooks.py` | **第一阶段新增**。HookBus 有三个时机：pre_tool_call、post_tool_call、on_tool_error。`required_check` 用来记录必经检查。默认 Hook 三个：`pending_write_notice`、`usage_completeness`、`recovery_plan` |
| `tooling.py` | 结构化错误码（类别、是否可重试、下一步）。**第一阶段新增 `recovery_for` 恢复决策表** |
| `policy.py` | 确定性风险门：auto、soft、hard、block，以及阶段写保护 |
| `store.py` | SQLite 业务适配器：商品、直播间、命令、审计、偏好、幂等账本 `action_ledger`、远端操作 outbox，事务内做版本和阶段重验 |
| `remote.py` | 独立 SQLite 仿真平台，支持故障注入（lost_response、delayed_visibility、before_commit、query_unavailable、reject）；包含 unknown→reconcile→manual_review 的对账流程 |
| `campaigns.py` | 备播 DAG：PlanSpec 校验、租约、原子提交、分支恢复、显式增量重规划 `replan`、角色级上下文；`_worker` 记录真实 checks |
| `specialists.py` | 可选的 LLM 手卡写手，带结构化重试和失败 usage 留存 |
| `advisor.py` | 只读建议和口播生成：引用、价格、SKU 和禁用词校验，最多修复一次 |
| `context.py` | 意图解析上下文；`mentions_sku` |
| `workspace.py` | 运行时 Reducer（阶段、观测、话题投影）、分阶段上下文包（4000 字符预算）、回答原文外置 |
| `memory.py` | L1 声明、L2 事实、L3 行为三层视图；窄范围的声明与行为冲突检测；显式遗忘 |
| `model_budget.py` | 持久化模型调用预留与对账账本（单指令、单计划、单直播间三级上限） |
| `feedback.py` | 建议的采纳、修改、拒绝反馈与会话指标，不等于执行授权 |
| `metrics.py` | 计划 Trace 的用量汇总 |
| `api.py` | FastAPI 路由。第一阶段新增 `GET /api/contracts` |
| `private_config.py` | 显式加载项目外的私有模型配置（不自动加载） |
| `web/` | 主播工作台：聊天、审批、备播、记忆、回执、故障注入 |
| 评测与演示脚本 | `benchmark.py`、`remote_demo.py`、`campaign_demo.py`、`operations_eval.py`、`multiagent_eval.py`、`real_model_eval.py`、`real_writer_repeats.py`、`conversation_eval.py`、`article_eval.py`、`context_eval.py` |

### 2.2 测试（`tests/`，共 195 项）

| 文件 | 覆盖 |
|---|---|
| `test_harness.py` | 审批、拒绝、过期、重启恢复、幂等、竞争改价、模型重试与降级、并发 |
| `test_remote_operations.py` | 远端发券的响应丢失、延迟可见、查询不可用、回执不符、重启 |
| `test_campaigns.py` | DAG 结构、并行重叠、租约、分支恢复、出口拦截、上下文与输出上限、记忆冲突 |
| `test_campaign_metrics.py` | 失败调用 usage 留存、usage 不完整的标注 |
| `test_conversation_upgrade.py` | 对话、指代、缺参、条件句、建议契约、偏好删除 |
| `test_article_harness.py` | 阶段 Reducer、审批跨阶段失效、预算并发与重启、增量重规划、反馈 |
| `test_contracts_hooks.py` | **第一阶段新增 35 项**：契约覆盖与自检、恢复表、远端只对账、SKU 被替换时要求澄清、削弱策略仍需审批、缺版本被拦截、故障 Hook 对写失败关闭、Hook 不能篡改、检查记录准确 |
| `test_multiagent_eval.py`、`test_real_model_eval.py`、`test_private_config.py` | 实验脚本与私有配置的离线测试（不调用真实 API） |

测试数量变化：33 → 71 → 83 → 89 → 125 → 160 → 195。简历里写的“89 项”已经过时。

### 2.3 数据

- `data/intent_eval_20261018.json`：24 条手写意图开发题，16 条普通、8 条挑战。是开发回归集，不是盲测。
- 业务种子数据（写在 `store.py`）：`LIVE-DEMO-001` 和 5 件护肤商品 SK-101 至 SK-105。
- `data/*.db`：各轮体验和检查产生的本地业务库、检查点库、平台库，用途见第 6.3 节。

### 2.4 排错与修复记录（有文件依据的）

| 问题 | 处理 | 来源 |
|---|---|---|
| 失败的模型调用丢失 usage，低估了消耗 | 失败 Trace 保留每次请求和已知用量 | `ENGINEERING_GAPS_AND_BUSINESS_PLAN.md` 第三节 |
| 商品售罄后无法重放同身份计划；开场偏好被普通切品误判为冲突 | 3 个回归用例，修复前失败、修复后通过 | `EVALUATION.md`“2026-10-16 自检” |
| 真实模型把“给面霜发10元券”自动补成 1 张；条件句被当成立即执行 | `_guard_instruction` 对照原话校验张数，条件、未来、否定句转为建议 | `REAL_MODEL_RESULTS_20261018.md` 第 1 节、`CONVERSATION_UPGRADE_20261019.md` |
| 建议生成里的价格表述未通过准入、JSON 包装错误、推荐列表被覆盖 | 改契约并补回归 | `CONVERSATION_UPGRADE_20261019.md` 第 4 节 |
| Trace 中的 hooks 是写死的名称列表 | 改为记录实际执行的 checks | `CONTRACTS_HOOKS_20261019.md` |
| 模型可以把用户点名的商品换成另一个 SKU 的写操作 | 用户点名商品时，写操作 SKU 必须是被点名的那个 | 同上 |
| 待审批响应缺少检查记录（LangGraph interrupt 会丢弃节点的部分状态） | 检查记录通过 interrupt payload 传出 | `agent.py` `_gate` 和 `_extract_response` |

### 2.5 第一阶段成果（本会话完成）

说明见 `CONTRACTS_HOOKS_20261019.md`，要点：

- `SKILLS` 改为由契约目录派生，`/api/skills` 输出不变；`BriefDraft` 移到 `contracts.py`，`campaigns` 仍会导出。
- 恢复决策表：结构错误只修结构；只读路径的暂态错误限次退避；400、预算耗尽、内容违规时停止；版本变化时刷新或重规划；远端超时只对账；多次查询仍未确认时转人工。写操作即使碰到暂态错误码，也改判为对账。
- 必经检查（带 `required: true`）写在代码路径里，不能卸载。可选 Hook 拿到的是事件副本，只能记录或拒绝；写操作前的 Hook 出异常时失败关闭，提交之后的 Hook 只能记录。

---

## 3. 当前状态

- **最后完成**：第一阶段。代码、35 项新测试、`CONTRACTS_HOOKS_20261019.md` 都已完成，README 已更新为 195 项。
- **进行中**：第二阶段刚开始设计就暂停了。准备修改 `context.py` 时，因为文件时间戳变化被拒绝写入，**没有任何第二阶段代码落盘**，也没有半成品。
- **后台任务**：没有本会话启动的后台任务或训练。本机有一个 uvicorn 服务：
  - 命令：`python -m uvicorn liveops.api:app --host 127.0.0.1 --port 8765`，PID 4827，下午 3:30 启动，监听 127.0.0.1:8765。
  - 按 `outputs/article_harness_rollout_20261019.json`，启动时为模型模式（`deepseek-flash`）、`simulated_remote` 发券后端，日志很可能写在 `data/interactive_server_20261019.log`。
  - 它使用哪个业务库和检查点库：**待核实**。本次查看进程环境变量的命令被中断。从文件修改时间推测是 `data/interactive_20261019.db`、`data/interactive_checkpoints_20261019.db`、`data/interactive_platform_20261019.db`，但这只是推测。
  - 该进程**早于第一阶段代码**，所以没有 `/api/contracts` 和 checks 记录。要体验新功能需重启，重启前先确认上面的库路径与环境变量。
- **任务列表中的待办**：阶段二（指代绑定、任务规格、失效判定）、反馈原因、场景评测集、隔离安全消融、文档与最终验证。全部为 pending。
- **2026-10-19 深夜补记**：已核实 PID 4827 使用 `data/interactive_20261019.db`、`data/interactive_checkpoints_20261019.db`、`data/interactive_platform_20261019.db`，`LIVEOPS_COUPON_BACKEND=simulated_remote`。服务仍未重启，仍是旧代码。阶段二至四的改动前备份在 `data/backups/phase2_20261019_205505/`。剩余工作：真实模型跑场景题（需授权 API 消耗）、长对话 6×60 轮实验、简历数字更新。
- **需要核实的文件改动**：第一阶段期间，有若干文件（`metrics.py`、`operations_eval.py`、`remote_demo.py`、`campaign_demo.py`、`benchmark.py`、`tests/test_campaign_metrics.py`、`tests/test_remote_operations.py`、`tests/test_harness.py`）显示过更新的修改时间，不是本会话改的；之后的 `ls -lt` 不再显示这些时间，原因**待核实**。改动前后全量测试都是 195 项通过。新会话可以先对比 `outputs/article_harness_tests_20261019.xml` 记录的测试和当前测试有无差异。

---

## 4. 实验结果

### 4.1 已验证结果（有原始记录）

| 结果 | 评测协议 | 来源 |
|---|---|---|
| 自动化回归 195 项通过 | `.venv/bin/python -m pytest -q`，离线，模型用 Mock | 本次实测；160 项版本见 `outputs/article_harness_tests_20261019.xml` |
| 真实模型三商品准备：串行中位数 2995.448ms，3 并发 1069.633ms（约 2.80 倍） | DeepSeek `deepseek-flash`，关闭思考，temperature=0；同一 DAG，workers=1 与 3 各 5 次，后 4 组交替先后；只有写手调用模型 | `REAL_MODEL_RESULTS_20261018.md` 第 2 节、`outputs/real_model_eval_20261018.json`、`outputs/real_writer_repeats_20261018.json` |
| 24 条意图题：规则 19/24，模型 20/24；普通指令规则 16/16、模型 14/16；挑战指令规则 3/8、模型 6/8 | 手写开发题，按动作、SKU、金额、数量联合匹配；不执行交易 | 同上第 1 节、`data/intent_eval_20261018.json` |
| 意图组件延迟 P50 952.181ms，最近秩 P95 1276.945ms | 24 题，含请求、解析、规范化 | 同上 |
| 54 次正式请求全部返回 usage：输入 18,658、输出 2,162、合计 20,820 token | 供应商报告值，不是账单 | 同上第 5 节 |
| 换品恢复：全量重建对比增量重规划，模型请求 3→1、任务执行 8→4、复用 0→4，耗时 1165.92ms 对 1139.99ms | 真实 DeepSeek 写手，同一五商品货盘，每种方案 1 次，单个开发场景 | `ARTICLE_HARNESS_20261019.md` 第 7 节、`outputs/article_harness_real_20261019.json` |
| 长对话指代：推荐后插入 8 轮离线查询，真实模型对“第二款”的绑定正确，8 项检查通过 | 2 条真实问题、4 次请求，中间 8 轮是离线填充 | `outputs/article_context_real_20261019.json` |
| 对话开发样例：9 项流程检查通过，20 次请求、19,740 token，响应中位数 2.218 秒 | 10 条人工设计的对话加 1 次准备链，不是独立测试集 | `CONVERSATION_UPGRADE_20261019.md` 第 4 节、`outputs/conversation_upgrade_verified_20261019.json` |
| 浏览器流程 15 项检查通过（两轮） | 浏览器脚本检查，离线规则后端 | `outputs/ui_upgrade_verified_20261019.json`、`outputs/article_harness_ui_20261019.json` |
| 交接字符 238,069→26,415，减少 88.904% | 40 个交接包，**对照组是全量共享，偏弱**；单位是字符，不是 token | `REAL_MODEL_RESULTS_20261018.md` 第 3 节、`outputs/multiagent_eval_20261018.json` |
| 分支恢复与整批重跑对比：任务尝试 140→90，减少 35.714% | 本地 10 次临时失败注入，不调用模型 | 同上 |
| 审核消融：45 个预设错误稿全部拦截，5 个正常稿通过 | 人为注入的固定错误，不是自然幻觉 | 同上 |
| 远端发券响应丢失：平台只提交一次，查询后才写本地 | 本地双 SQLite 仿真 | `outputs/remote_recovery_demo.json` |
| 离线容量：1/4/8 worker 吞吐中位数 227.65/362.53/332.17 次每秒；8 并发比 4 并发更慢 | 直接调用 Agent，不经 HTTP、不调模型，每档 3 轮 × 100 次 | `ENGINEERING_GAPS_AND_BUSINESS_PLAN.md` 第四节。`outputs/operations_eval_20261017.json` 在 10-19 被重跑覆盖过，**当前文件内的数值待核实是否与文档一致** |

### 4.2 初步趋势（样本小，不能当结论）

- 模型擅长口语和边界指令，但普通指令反而不如规则稳定。可以考虑“规则快路径加模型兜底”，但还没有实现路由，也没有评测。
- 增量重规划能减少模型调用和任务执行，但单次实验里耗时没有明显变化，因为全量重建本身就是并行的。
- 结构化列表能让“第二款”跨轮次绑定正确，但只测过 1 个场景。

### 4.3 未验证的想法

- 指代绑定列表ID、来源轮次和计划版本后，能减少多方案并存时的误绑定。
- 结构化任务规格能更准确地判定需求变更后的失效范围。
- 在隔离消融中，各防线的首个拦截分布。
- 规则与模型混合路由的意图准确率和成本。
- 分层压缩在 60 轮会话中的约束保留率。

---

## 5. 剩余工作

| 优先级 | 事项 | 依赖 | 阻塞或说明 |
|---|---|---|---|
| P0 | 新会话开始前：核实第 3 节的进程库路径和文件改动来源 | 无 | 防止误删、误覆盖正在体验的数据 |
| P1 | **阶段二：指代与任务规格** | 第一阶段（已完成） | 无阻塞。设计见下 |
| P1 | **阶段三：场景评测集与运行器** | 阶段二（变更类样本依赖规格层） | 真实模型部分会消耗 API，需控制次数 |
| P1 | **阶段四：隔离安全消融** | 第一阶段契约和 Hook；阶段三的样本格式 | 只能在评测脚本里用 patch 关闭某层，不能改服务 |
| P2 | 反馈原因和替代方案 | 无 | 在 `FeedbackIn` 加可选字段 `reason`、`alternative`，按原因统计；不做信任分 |
| P2 | 文档、README 和简历更新 | 阶段三、四出数字之后 | 简历数字只能来自实测 |
| P3 | 生产缺口：鉴权与审批人绑定、多租户、服务级并发和排队、异步 worker、真实平台契约 | 有真实部署需求时 | 当前定位是本机演示，暂不做 |
| 暂缓 | 信任分、GitLab、向量库、Planner 训练、Redis/Kafka | 有真实业务反馈和数据时 | 用户明确暂缓 |

### 5.1 阶段二设计（规划，未实现）

1. 在 `workspace.reduce_state` 的话题投影中，把 `listed_skus` 扩展为 `lists` 数组，每项包含：
   - `list_id`：由内容和来源指令计算的哈希；
   - `source_command_id`；
   - `skus`；
   - `constraints`：预算、数量、范围；
   - `plan_ref`：备播计划的 `run_id`、`revision`、当前 SKU；
   - `status`：active、alternative 或 superseded。

   保留 `listed_skus`，让它指向当前生效列表，兼容旧代码（`agent._rule_action`、`context_eval.py`、`web/app.js` 都依赖它）。
2. `context.py` 新增 `resolve_ordinal`，被 `_rule_action` 和 `_guard_instruction` 调用：
   - 只有一份有效列表时直接绑定；
   - 多份并存时，必须由用户说明是哪一份，例如“新方案”“第一份”，否则要求澄清；
   - 引用已被替换的旧方案时要求澄清；
   - 对应计划已经重规划时，要求按商品名称或 SKU 指定；
   - 解析结果写入动作的 `reference` 字段。
3. 新增 `RequestSpec`，包含目标、范围、约束、交付物、权限边界。plan 和 prepare 先生成规格，经代码校验后再选用现有模板。
4. 比较新旧规格，生成失效报告：
   - 只改风格：重写受影响的手卡；
   - 改预算或范围：旧列表标为 superseded，重新选品；
   - 改金额或数量：生成新提案，旧审批作废；
   - 绝不替用户自动修改金额、SKU 或数量。
5. 写操作带有 `reference` 时，在提交事务内确认引用的列表仍然有效，否则返回状态冲突。

验收：
- 隔多轮仍能解析“第二款”；
- 两份方案并存时要求澄清；
- 换预算后旧序号不可再用；
- 旧方案上的审批不能转移给新商品；
- 重规划后的序号要求澄清；
- 原有 195 项测试全部通过。

---

## 6. 复现与接续

### 6.1 环境

- macOS（Darwin 24.6），项目根目录 `/Users/wumao/Downloads/项目报告汇总/liveops-harness`，**不是 git 仓库**（没有版本历史可回滚，改动前建议手动备份）。
- Python 3.11.13，虚拟环境 `.venv/`。`pyproject.toml` 只写了依赖范围，没有锁定版本；当前安装的版本见第 0 节。
- 重建环境：`python3.11 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'`。
- 模型配置：私有文件 `~/.config/liveops-harness/deepseek.env`（目录权限 700、文件 600，**不要读取或打印内容**）。配置项名称见 `.env.example`，另外支持 `LIVEOPS_SPECIALIST_MODEL`。默认离线运行，必须显式加载私有配置才会调用模型。

### 6.2 关键命令

```bash
cd "/Users/wumao/Downloads/项目报告汇总/liveops-harness"

# 离线回归（不调用模型）
.venv/bin/python -m pytest -q

# 离线演示与机制实验
.venv/bin/python -m liveops.campaign_demo        # 写 outputs/harness_v2_demo.json
.venv/bin/python -m liveops.remote_demo          # 写 outputs/remote_recovery_demo.json
.venv/bin/python -m liveops.benchmark --requests 100 --workers 1 4 8
.venv/bin/python -m liveops.operations_eval      # 会覆盖 outputs/operations_eval_20261017.json

# 离线启动服务（端口被占用时不要重复启动）
.venv/bin/python -m uvicorn liveops.api:app --host 127.0.0.1 --port 8765

# 加载私有模型配置启动（会产生 API 消耗）
.venv/bin/python -m liveops.private_config -m uvicorn liveops.api:app --host 127.0.0.1 --port 8765

# 真实模型实验（会消耗 API；部分脚本会覆盖同名结果文件）
.venv/bin/python -m liveops.conversation_eval    # 最多约 32 次请求
.venv/bin/python -m liveops.article_eval --real-provider --output outputs/article_harness_real_repeat.json
```

注意：`real_model_eval.py` 再次运行会覆盖 `outputs/real_model_eval_20261018.json`；`real_writer_repeats.py` 发现已有结果文件会拒绝运行。

### 6.3 断点与数据库路径

| 用途 | 路径 |
|---|---|
| 默认业务库 / LangGraph 审批断点 | `data/liveops.db` / `data/checkpoints.db`（可用 `LIVEOPS_DB_PATH`、`LIVEOPS_CHECKPOINT_PATH` 修改） |
| 默认仿真平台库 | 业务库同目录下的 `simulated_platform.db`（可用 `LIVEOPS_PLATFORM_PATH` 修改） |
| 交互体验库（推测为运行中服务所用，待核实） | `data/interactive_20261019.db`、`data/interactive_checkpoints_20261019.db`、`data/interactive_platform_20261019.db` |
| 文章升级 UI 检查库 | `data/article_ui_20261019.db`、`data/article_ui_checkpoints_20261019.db`、`data/article_ui_platform_20261019.db` |
| 早期升级检查库 | `data/upgrade_*_20261019.db` |
| 备份 | `data/backups/upgrade_20261019/`、`data/backups/article_upgrade_20261019_153052/` |
| 备播计划断点 | 业务库内的 `campaign_runs`、`campaign_artifacts` 表 |
| 运行时状态 | 业务库内的 `runtime_states`、`runtime_events`、`runtime_turns`、`runtime_artifacts` 表 |
| 模型账本 | 业务库内的 `provider_scopes`、`provider_calls` 表 |
| 服务日志 | `data/interactive_server_20261019.log`、`data/server.log` 等 |

### 6.4 新会话的下一步操作

1. 读本文件和 `CONTRACTS_HOOKS_20261019.md`、`CLAUDE_PLAN_SIMPLIFIED_20261019.md`。
2. 运行 `.venv/bin/python -m pytest -q`，确认是 195 项通过。
3. 核实 8765 服务所用的数据库路径（例如运行 `LC_ALL=C ps eww -p 4827`，只看 `LIVEOPS_*_PATH` 和 `COUPON_BACKEND`，不要打印 key）。暂时不要重启它。
4. 修改前先备份要改的源文件（项目不是 git 仓库）。
5. 按第 5.1 节实现阶段二，每一步都跑全量测试，完成后写 `REFERENCE_SPEC_*.md`。
6. 再依次做阶段三、阶段四和反馈原因，最后统一更新 README 和测试数量。

---

## 7. 注意事项

### 7.1 已知问题

- 运行中的 8765 服务是旧代码，见第 3 节。
- `build/lib/` 是旧的构建缓存，缺少新模块，不要当作项目源码（`PROJECT_INTEGRITY_CHECK_20261019.md`）。
- 文档里有大量本机绝对路径，换机器后链接会失效。
- 契约中的超时只是声明，本地 SQLite 工具没有执行器级的强制超时；准备链的 30 秒是协作式检查，不会强杀线程。
- 只有 `127.0.0.1` 监听，没有鉴权；知道 `command_id` 就能审批。**不能暴露到公网。**
- 模拟库存和故障注入接口只用于演示，真实环境必须关闭。
- 只在服务级做了单进程验证；SQLite 检查点有串行化，不能据此声称多实例高并发。

### 7.2 不能混用的实验版本

- 真实模型结果（10-18、10-19 的 DeepSeek 实验）不能和离线 Mock 或模拟延迟的结果放进同一张表。例如模拟 40ms 等待得到的 182.521→74.708ms，不能和真实的 2995→1070ms 混在一起。
- 测试数量要和日期对应：89（10-18）、125（10-19 上午）、160（10-19 下午）、195（10-19 晚）。
- `outputs/operations_eval_20261017.json` 在 10-19 被重跑覆盖，文件名中的日期不代表数据时间。
- 第一阶段修改了源码，旧实验 JSON 里记录的源码 SHA256 与当前代码不一致。这是正常的，但不能说旧实验是在当前代码上跑的。
- `outputs/harness_v2_demo.json` 在第一阶段验证时被重新生成过（离线），trace 结构从 `hooks` 改成了 `checks`。

### 7.3 不能夸大的结论

- 不能说“意图准确率 83%”或“模型优于规则”：只多对 1 题，普通指令上模型更差，而且是开发集。
- 不能说“并行提速 2.8 倍是质量提升”，也不能把它当作生产 P95；它只是 5 次小样本中位数。
- 不能说“增量重规划显著提速”：耗时接近，只测了 1 个场景。
- 不能说“上下文 token 节约 88.9%”：单位是字符，对照组是全量共享。
- 不能说“合规认证”：只是字段对照和关键词检查。
- 不能说“记忆提升效果”：只验证了 8 项行为契约，没有效果实验。
- 不能说接入了淘宝、有真实成交或 GMV 提升、做过多租户或线上压测。
- 不能说是“四个自主大模型协作”：准备链里只有写手调用模型，选品、审核、汇总都是确定性代码。
- 不能把文章里的数字（0.847、0.737 等）当作本项目结果。
- 采纳率不等于正确率，也不等于授权等级。

### 7.4 相关文档索引

`README.md`、`ARCHITECTURE.md`、`HARNESS_UPGRADE.md`、`HIGHLIGHTS_AND_TRADEOFFS.md`、`ENGINEERING_GAPS_AND_BUSINESS_PLAN.md`、`EVALUATION.md`、`MULTI_AGENT_EXPERIMENTS_20261017.md`、`REAL_MODEL_RESULTS_20261018.md`、`CONVERSATION_UPGRADE_20261019.md`、`ARTICLE_HARNESS_20261019.md`、`CLAUDE_PLAN_SIMPLIFIED_20261019.md`、`CONTRACTS_HOOKS_20261019.md`、`PROJECT_INTEGRITY_CHECK_20261019.md`、`INTERVIEW_GUIDE.md`、`INTERVIEW_MEMORY_25MIN.md`（面试速记，文档备份在 `outputs/document_backups/`）、`HANDOFF_20261019.md`（本会话较早写的简版交接，内容已并入本文）。
