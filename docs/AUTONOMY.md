# 模型自主性改进路线

本文对照公开的同类项目，评估当前架构距离“更强自主性”还差什么，并给出可分阶段落地的改进清单。
外部页面在当前环境无法直接抓取，文中对照依据检索到的项目文档标题与公开架构描述，链接集中在文末，便于复核。

## 结论

当前机器人已经具备“每次消息触发的一次性自主”：理解意图、选择工具、决定静默、维护认知。它还不是“持续存在的自主”：

1. **没有时间维度**：只有新消息能唤醒主 agent，无人说话时什么都不发生。
2. **没有跨回合目标**：未完成的工作只存在于持续会话上下文里，预算耗尽或会话重置即丢失。
3. **没有结果反馈**：机器人不知道回复之后群友是接话、无视、纠正还是反感，因此无法变聪明。
4. **没有程序性记忆**：会记事实，不会记“这类事情应该怎么做”，也没有可复用的技能。
5. **能力面固定**：工具在代码里注册，扩展要靠改代码；子代理入口在聊天回合中被关闭。

因此提升自主性的主线不是“给模型更多自由”，而是补齐三件事：**触发（时间）、连续性（目标）、学习（结果）**。
QQ 群是社交场景，错误发言的代价远高于少说一句，所以应遵循“先可观察，再可发言；先私聊，再群聊；先只读，再写入”的顺序。

## 目标形态

把“消息 → 回合”的单次链路，补成“事件 → 唤醒 → 回合 → 结果 → 议程”的闭环：

```text
消息 / 议程到期 / 心跳 / 反思定时
        ↓
唤醒路由 Wake（来源 + 理由 + 授权快照）
        ↓
权限与预算闸门（等级、配额、静默时段）
        ↓
主 agent 回合（工具、记忆、发送或静默）
        ↓
结果信号 outcomes
        ↓
反思 / 技能 / 策略（人工确认后才生效）
        ↓
新的议程
```

这条闭环里，模型负责“想做什么、怎么做”，程序负责“能不能做、做了多少、做完记什么”，维护者负责“是否允许继续”。

## 一、现状评估

### 1. 已经做对、不要推翻的部分

| 能力 | 落点 | 为什么是自主性的地基 |
| --- | --- | --- |
| 按机器人/群/私聊持续恢复的原生会话 | `plugins/assistant/dsh.py` 的 `Turn` | 长上下文连续性，是不重复提问、不丢线索的前提 |
| 增量背景对比 | `dsh_context.mjs` | 只传变化内容，长期会话才可能持续 |
| 工具白名单 + 执行器鉴权 | `tools.py`、`modules.py` | 自主性扩张时不至于变成任意操作 |
| 发送事务、去重、过期检查 | `service.py`、`groupchat.py` | 自主发言越多，幂等和防重越重要 |
| 认知来源、版本检查、人工固定保护 | `memory_tools.py`、`consolidation.py` | 记忆可被自主维护，又不会被人格漂移带走 |
| 每回合共享预算（调用/Token/时间/费用） | `budget.py` | 自主性的天然刹车 |
| 审批、确认码、复测发布 | `plugins/tasks`、`schedule.py` | 高风险动作的人工闸门 |
| 模块事件总线 | `events.py` | 让 agent 感知真实执行状态，而不是只读聊天 |
| 脱敏诊断日志 | `trace.py`、`docs/LOGGING.md` | 自主行为的可回溯基础 |

这些机制在同类开源项目里并不常见。改进应建立在它们之上，而不是替换成另一套 agent 框架。

### 2. 真正限制自主性的八个点

| 编号 | 限制 | 现状证据 | 导致的后果 |
| --- | --- | --- | --- |
| A | 唤醒只来自消息 | `docs/DSH.md` 设计段；提示词 `chat.modules` 明确“当前没有无人发消息时自主安排唤醒的能力” | 没有“稍后回来”“过一会儿跟进”“夜里整理” |
| B | 无持久目标/议程 | `Assistant.handle` 以 `message_id` 为入口；回合超时 600 秒 | 复杂任务只能一口气做完，做不完就断 |
| C | 无结果信号 | 发送成功后只归档文本，不记录后续反应 | 参与策略、语气、时机都无法从经验中调整 |
| D | 检索是关键词/二元组 | `docs/DEVELOPMENT.md`“不依赖向量库”；`history_tools.py` 用 `instr(lower(content), ...)` | 换个说法就查不到，记忆“有但取不出来” |
| E | 无技能/程序性记忆 | `prompts/modules.json` 只有静态提示词；插件要写 Python | 同类任务每次重新摸索，经验不沉淀 |
| F | 工具面固定，子代理关闭 | `dsh.py` 的 `entries` 关闭 `tool-subagent`、`tool-subagent-fork`、`tool-workflow`、`tool-ralph`；桥接 `tools.restrict({allow: []})` | 无法把长调研、并发探索分出去 |
| G | 参与策略固定 | `topics.py` 按话题人数和对机器人说法的回应限制参与 | 尚不能从反馈自动学习参与时机 |
| H | 没有评估与回放 | 测试以 MockTransport 为主，提示词改动无对照 | 越自主越不敢改，改坏了也发现不了 |

补充两个隐性成本问题：

- **上下文只增不减**：持续会话 + 1,000,000 Token 预算 + 无压缩，长会话会先耗尽预算再回答问题。同类项目（Claude Code 的 agent loop、Anthropic 的 context engineering）都把“压缩/清理工具结果”当作一等公民。
- **每回合变化的内容放在 system 前缀里**：`tools.py` 把预算、身份、模块说明拼进 `messages[0]`，桥接每步重发 `system`。这会削弱服务商的前缀缓存收益。把动态内容移到消息尾部，可降低长会话成本（用 `dsh_usage` 的缓存命中字段验证，不要凭感觉改）。

## 二、外部对照：它们各自解决了什么

| 项目/资料 | 关键做法 | 对本项目的启示 | 不要照搬 |
| --- | --- | --- | --- |
| [MaiBot](https://docs.mai-mai.org/develop/architecture/expression-learning) | 群聊为核心的常驻智能体；把“表达方式”从人格正文里拆出来单独学习 | 表达/口癖可以作为独立记忆层，按群积累、按场景取用 | 不要让人格正文被自动改写 |
| [MaiWork](https://github.com/CharTyr/MaiWork) | 补全群聊常驻 Agent：理解一个群、主动找资讯、提构想、追目标、交付工作 | 正是“议程 + 目标 + 主动投递”的方向；可借鉴“按群建档、按目标推进” | 不要做成无预算的资讯推送 |
| [Mai_love](https://github.com/ab-Royo/Mai_love) | 主动关心型交互 | 私聊先做主动跟进，风险远低于群聊 | 不要用固定模板“每日问候”刷存在感 |
| [AstrBot MCP](https://docs.astrbot.app/use/mcp.html)、[SubAgent](https://docs.astrbot.app/use/subagent.html) | 通过 MCP 扩展工具；子代理编排 | 工具面可插拔；复杂任务可拆分 | 不要默认给任意 MCP server 全权限 |
| [LangBot 知识库](https://docs.langbot.app/zh/usage/knowledge/readme)、[AgenticRAG](https://space.langbot.app/market/langbot-team/AgenticRAG) | 检索增强与 agent 化检索 | 检索应“由模型决定查什么、查几次”，而不是固定召回 | 不要引入独立向量数据库运维 |
| [XiaoZou-Bot](https://github.com/fayev1t/XiaoZou-Bot) | 基于 Agent Loop / Harness 架构的 QQ 机器人 | 你已经在 DSH 原生循环上，架构不落后 | 不必为了“像 agent”而重写循环 |
| [Letta Memory & dreaming](https://docs.letta.com/configuration/memory) | 记忆块 + 后台“做梦”式整理 | 睡眠整理是低成本提升记忆质量的手段 | 不要让整理结果直接覆盖人工固定值 |
| [Generative Agents](https://arxiv.org/abs/2304.03442) | 记忆流 + 反思 + 计划；检索按新近度/重要性/相关性排序 | 记忆要有“重要性”和“最近使用”，反思要能生成更高层结论 | 不要模拟整座小镇的社交仿真 |
| [Voyager](https://arxiv.org/abs/2305.16291) | 技能库 + 自动课程 | 技能是程序性记忆，可复用、可组合 | 不要让技能获得新权限 |
| [Reflexion](https://arxiv.org/abs/2303.11366) | 用自然语言总结失败教训，下次带进上下文 | “教训”比“事实”更能改变行为 | 不要无证据地自我归因 |
| [Claude Code: skills/hooks/subagents](https://claude.com/blog/steering-claude-code-skills-hooks-rules-subagents-and-more)、[agent loop](https://code.claude.com/docs/en/agent-sdk/agent-loop) | 技能按需加载；子代理隔离上下文；压缩与工具清理 | 技能用“元数据先暴露、正文按需加载”的渐进披露 | 不要把技能写成会执行的代码 |
| [Context engineering](https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools) | 压缩、记忆工具、清理旧工具结果 | 长会话必须有压缩策略 | 不要靠删历史“解决”上下文 |
| [MCP 规范](https://modelcontextprotocol.io/specification/2025-06-18) | 标准化的工具/资源/提示协议 | 未来接外部能力的低成本路径 | 不要把 MCP 结果当可信指令 |
| [ProACT](https://arxiv.org/abs/2607.03730)、[Speak for Me](https://www.semanticscholar.org/reader/44323f6384c34142fe4d32492bc793c628c317a9) | 多用户协作中的主动介入时机 | “什么时候说”本身是独立能力，值得单独建模 | 不要只用一个积极性阈值代替 |
| [Heart](https://zenodo.org/records/20098021) | 零 Token 心跳架构 | 心跳先用规则/事件预过滤，再决定是否花模型调用 | 不要让心跳每 tick 都调大模型 |
| [OpenAI Agents SDK](https://pypi.org/project/openai-agents/) | guardrails、handoffs、sessions | 护栏与交接应是框架级能力 | 不要为了 handoff 引入第二套运行时 |
| [AG2 GroupChat](http://classic.docs.ag2.ai/latest/docs/user-guide/advanced-concepts/groupchat/groupchat/) | 多智能体群聊编排 | 可借鉴“角色分工”，但成本高 | 私人小群不需要多 agent 角色扮演 |

## 三、自主性分级：把“更自主”变成一个可调旋钮

不要用布尔开关。建议按群（私聊单独一档）配置自主等级。等级是叠加的：**消息应答始终开启**，等级只决定“在应答之外还能做什么”，且**不改变工具鉴权和发起人权限**。

| 等级 | 名称 | 在“消息应答”之外新增的行为 | 默认 |
| --- | --- | --- | --- |
| L0 | 仅应答 | 无，与当前一致 | 当前默认 |
| L1 | 静默整理 | 空闲时读取、整理记忆、写反思报告；不新增发言 | 推荐首先开启 |
| L2 | 续话 | 在自己参与过的话题内补充、追问、稍后回访；有每日配额 | 私聊可先开 |
| L3 | 主动发起 | 可按议程在群内主动发言（含无人消息时）；严格预算、时段与审核 | 默认不开放 |
| L4 | 自主运营 | 可提议技能/插件/设置、可委托研究型后台任务；仍需人工确认 | 需明确批准 |

配套字段（写入 `group_chat.json` 或后台设置，按群覆盖）：

```json
{
  "autonomy": {
    "enabled": true,
    "level": 1,
    "shadow": true,
    "daily_turns": 8,
    "daily_tokens": 200000,
    "daily_cost": 0,
    "quiet_hours": ["23:30", "08:00"]
  }
}
```

`shadow=true` 时所有自主决策只记录不发送，用于观察唤醒精度。这是从 L1 走向 L2/L3 的必要阶段。

## 四、改进清单

按优先级分为 P0（地基）、P1（可见自主）、P2（能力扩张）。每项给出问题、设计、落点、验收和护栏。

### P0-1 唤醒抽象层与议程账本

**问题**：`Assistant.handle` 以用户消息为唯一入口（`message_id`、`user_id`、`send`），无法表达“我自己决定现在做一件事”。

**设计**：

- 新增 `Wake` 抽象：`source`（message / intention / heartbeat / reflection / task）、`scope`、`request_id`、`reason`、`evidence`、`grant`（发起人及权限快照）、`budget`。`grant` 只用于核验，不能由唤醒来源自行扩大。
- 新增 `intentions` 表（`data/memory.sqlite3` 或独立 `data/autonomy.sqlite3`）：
  `id, bot, scope, kind, due_at, condition, payload, status, attempts, max_attempts, budget, created_by, source_request, rationale, created, expires, result`
  - `kind`：`follow_up`（自己发言后回访）、`remind`（定时提醒）、`watch`（等一个条件）、`research`（后台调研）、`consolidate`（整理）。
- 新工具：`set_intention`、`list_intentions`、`cancel_intention`。创建时就写入依据（消息 id / 事件 id）和理由，便于后台审计。
- 空闲工作循环（可复用 `__init__.py` 现有 `plugin_loop`/`cleanup_loop` 的形态）扫描到期议程，构造 `Wake`，走新的 `handle_wake` 入口。
- 归档：自主回合产生的消息以 `usr=bot`、`metadata={"wake": ..., "intention": ...}` 写入；合成触发 id 用独立负数计数器，避免与真实 `message_id` 去重冲突。

**验收**：

1. 手动插入一条已到期 `follow_up`，重启机器人后仍能触发；
2. 无人发消息时可完成一次只读总结并写入模块事件；
3. `shadow=true` 或 L0/L1 时，自主回合不产生 QQ 消息；
4. 取消议程后不再触发，失败按退避重试且不超过 `max_attempts`。

**护栏**：每会话最多 1 条待执行 `follow_up`；发言类议程必须 L2+（群内主动发起需 L3+）；创建发言类议程需要来源请求或维护者预先授权；到期时重新读取上下文，话题已结束则自动放弃。

### P0-2 自主性开关、预算与审计面板

**问题**：自主行为一旦出现，必须能一键关、能看清单、能算成本，否则无法长期运行。

**设计**：

- 配置：等级、影子模式、每日回合/Token/费用上限、静默时段、允许的群、总开关。沿用 `.env` + 后台设置 + `group_chat.json` 的分层覆盖。
- `autonomy_events` 表：每次自主回合记录 `wake_id, source, reason, level, tools, tokens, cost, action, outcome, vetoed`。`wake_id` 同时写入 `trace.py` 事件和 `events.py`，形成可检索链路。
- 后台新增“自主性”页：时间线、按群统计、失败原因、被否决记录、手动取消议程、暂停/恢复。
- 每群显示“最近一次自主回合为什么发生”，可点开依据消息。
- 聊天侧提供 `/自主性` 查看与调整本群等级（复用现有群管理权限），后台可覆盖；默认值始终是 L0/L1，上调需要明确操作。

**验收**：总开关关闭后 10 秒内不再产生自主回合；面板能看到每次自主回合的原因、花费和结果；被否决的决策进入下一轮反思素材。

### P0-3 上下文与成本卫生

**问题**：持续会话只增不减，长会话会先耗尽预算；动态内容破坏前缀缓存。

**设计**：

- **稳定前缀**：`system` 只保留稳定规则与人设；预算剩余、当前身份、任务状态改放到消息尾部的“本轮状态”块（或独立低优先级段）。改完用 `dsh_usage` 的缓存字段对比，不凭感觉。
- **压缩**：当会话事件数或估算 Token 超过阈值时，生成“会话纪要”（已作出的决定、未完成线索、用户偏好、重要锚点 id），旧工具结果替换为一行摘要，保留可回查的归档 id。
- **工具结果清理**：保留最近 K 条完整结果，更早的只留状态与关键字段（同类项目普遍这么做）。
- **预算尾部化**：每轮把 `budget.remaining()` 作为一条尾部状态注入，而不是改 system。

**验收**：100 轮会话仍能在预算内完成回答；纪要在后台可读、可编辑；压缩前后同一问题的答案质量无明显下降（用评估集对比）。

### P0-4 结果信号采集（只读）

**问题**：没有“说完之后发生了什么”，就没有学习。

**设计**：

- 对每条机器人消息记录后续窗口（如 10 分钟）内的：有人回复、有人引用、有人复读、有人拍一拍、机器人被点名、用户纠正（“不是这个意思”）、明确赞赏/抱怨、以及无人理会。
- 新增 `outcomes` 表：`message_id, scope, features(json), label, collected_at`。**只存聚合特征与判定，不存新的聊天正文**；正文仍在原归档。
- 判定优先用规则（引用/复读/拍一拍可确定性识别），语义判定（纠正/赞赏）交给辅助模型，低置信度标 `unknown`。
- 不把结果直接写进人格或提示词；先进入反思报告，由维护者决定是否采纳。

**验收**：后台能看到单条回复的后续反应；统计页能给出“被接话率/被无视率”；关闭采集后不产生新记录。

### P1-5 空闲心跳（先影子）

**问题**：没有时间维度的自主。

**设计**：

- **零 Token 预过滤**：到期议程、未处理的点名、距上次唤醒超过阈值、自上次唤醒后的新消息、失败发送、刚结束的后台任务、长时间静默。任一命中才进入下一步。
- **轻量分诊**：辅助模型只输出 `{"act":bool,"reason":"...","kind":"..."}`，输入是压缩后的状态，不是全量聊天。
- **完整回合**：分诊通过后构造 `Wake(source=heartbeat)`，共享预算，但**使用独立并发额度**，不能挤占用户请求的 2 个槽位。
- **约束**：全局每日上限、每群最小间隔、静默时段、用户回合在途时跳过、失败熔断。
- **影子模式**：只记录“如果允许，我会做什么”，不发消息；维护者在后台标注有用/无用，累计精度达标后再升级等级。

**验收**：影子模式连续 3 天，人工标注“有用”占比 ≥ 60% 才允许上调等级；心跳空转（分诊拒绝）不产生模型费用。

### P1-6 续话与提醒

**问题**：主动性只有“回不回当前消息”，没有“稍后”。

**设计**：

- `follow_up`：自己发言后若 N 分钟无人接话，或对方承诺“我查一下”后超时，可决定补一句或私聊提醒；到期先重新判断相关性。
- `remind`：用户明确要求“明天提醒我”时创建，发送前确认目标会话仍在。
- `watch`：等待关键词/话题/某人发言，命中后唤醒一次，不重复。
- 配额：每群每日 3 条、每机器人每日 20 条；同一话题同时最多 1 条；全部可在后台看到并取消。

**验收**：机器人能“过 30 分钟回来补一句”，旧话题已结束时自动放弃；连续 20 条配额用尽后不再创建。

### P1-7 睡眠整理 / 反思

**问题**：记忆只增不减，矛盾不处理，经验不沉淀。

**设计**：

- 夜间或空闲超过 2 小时触发一次离线整理（不占用聊天并发）：
  1. 合并重复认知，标出矛盾（不擅自判定谁对）；
  2. 生成“今天哪些回应有效、哪些被无视、哪些操作失败”的教训；
  3. 提出记忆修改建议（diff，不直接写）；
  4. 更新群摘要/群认知的建议稿；
  5. 记录待跟进事项（转为 `intention` 建议，需确认）。
- 产物写入 `data/reflections/YYYY-MM-DD.json`，后台可读、可采纳/拒绝；人工固定值永不自动覆盖。
- 采纳的“教训”进入一个独立的经验库（见 P1-9 技能库），按场景检索，不常驻提示词。

**验收**：每天早上有一份可读报告；采纳/拒绝都有记录；拒绝后同类建议不重复轰炸。

### P1-8 检索升级

**问题**：关键词匹配换个说法就失效，记忆“有但取不出来”。

**设计**：

- **SQLite FTS5** 虚拟表覆盖消息正文（中文用 `trigram` 分词；已在本机 `.venv` 的 SQLite 3.49.1 验证 FTS5 与 trigram 可用），保留现有 `instr` 作为兜底；零新增依赖。
- **排序**：相关性 + 新近度 + 重要性（被点名、提问、纠正、长度、是否机器人相关）+ 最近使用次数/频率，参考 Generative Agents 的检索打分。
- **可选向量**：配置化 embedding（复用现有服务商或本地模型），向量存 SQLite blob 表；不可用时自动回退 FTS，不阻塞启动。
- **时序有效性**：`facts` 增加 `valid_from`、`valid_to`、`supersedes`，支持“当时是怎么认为的”，矛盾保留双方并用时间区分。
- **检索由模型决定**：使用独立的历史查询工具，增加 `search_memory` 的语义入口；结果标注来源与时间。

**验收**：20 条语义改写查询的命中率明显提升；未配置 embedding 时功能不降级；删除来源后派生记忆仍失效。

### P1-9 技能库（声明式程序性记忆）

**问题**：会记事实，不会记“这类事情怎么做”；扩展只能写 Python。

**设计**：

- `skills/<name>/SKILL.md`，frontmatter：`name`、`description`、`when_to_use`、`tools`（必须是当前会话工具的**子集**）、`risk`、`examples`、`version`。正文是步骤、判断标准、反例。
- **渐进披露**：提示词里只放名称与一句话描述；模型判断相关后调用 `load_skill` 取正文，避免常驻上下文。
- **作者通道**：`propose_skill` 只生成建议（diff），后台展示前后差异，维护者确认后才落盘；技能不能声明新工具、不能带可执行代码、不能扩大权限。
- **沉淀**：复杂任务成功完成后，模型可提议把过程固化为技能；记录使用次数与成功率，长期失败的建议淘汰。
- **能力缺口检测**：同类请求多次无法用现有工具完成时，提议“需要什么技能/插件/设置”，而不是勉强编造答案；提议进入后台待审，不自动创建任务或改配置。
- 表达层可作为技能特例：某群的梗、口癖、成功措辞，按群存储、低权重注入，人工可禁用。

**验收**：新增技能不需要改 Python；技能只能调用当前会话已有工具；被拒绝的技能不可加载；技能加载失败不影响正常聊天。

### P1-10 计划/待办工件

**问题**：多步任务的过程只存在于上下文，预算耗尽就断。

**设计**：

- `set_plan` 工具写入小型计划（步骤、状态、验收点），每轮以紧凑块注入；完成后自动归档；过期清理。
- 计划与 `intention` 配合：计划中未完成步骤可转为 `follow_up`，实现跨回合续做。
- 后台可查看当前计划，维护者可取消。

**验收**：多步任务中计划状态与实际工具调用一致；回合中断后下轮能接着做；完成后不残留。

### P2-11 MCP 客户端

**问题**：工具面固定，接外部能力要改代码。

**设计**：

- 允许列表式 MCP server（本机 stdio 优先），按 server 配置可用工具与作用域；默认只读，写入需单独授权。
- 工具结果一律标记为不可信资料；注入样例测试必须覆盖“网页/工具结果要求发消息”不得触发发送。
- 不在聊天回合暴露任意文件系统/命令工具。

**验收**：只读 MCP server 可接入；未授权 server 不可见；注入样例无法产生发送副作用。

### P2-12 只读研究子代理 / 研究型后台任务

**问题**：长调研会占满主回合预算，且没有并行探索。

**设计**：

- 优先复用 `plugins/tasks`：新增“研究型任务”配置（只读工具、Token 上限、交付报告），不执行代码；主 agent 委托后继续聊天，结果以事件返回。
- 次选才是打开 DSH 原生子代理入口，并限制为只读工具与独立预算（`dsh.py` 的 `entries` 目前显式关闭，改动需评估）。
- 子任务失败不得改变主会话状态；结果必须可追溯。

**验收**：主会话不被长查询占满；子任务结果可查；失败只产生明确状态。

### P2-13 评估与回放

**问题**：越自主越不敢改提示词和工具，因为无法验证。

**设计**：

- 30–50 条金标准场景：群聊接梗、被纠正、隐私试探、注入尝试、任务路由、定时确认、记忆冲突、图片理解、静默判断。
- 从 `logs/model/trace-*.jsonl` 回放；影子模式下对比“旧规则 vs 新规则”的决策（回复/静默/工具选择），质量用 LLM 评审 + 人工抽查。
- 指标：唤醒精度、静默正确率、工具成功率、每有效回复成本、议程完成率、审批通过率、事故数。
- 每次提示词/工具变更必须跑一遍，结果存档。

**验收**：改动前后可对比；回归失败能定位到具体场景；指标进入后台。

### P2-14 参与策略学习与表达记忆

**问题**：当前固定话题策略不能从反馈中学习时机；后续学习不得绕过人数和续话门槛。

**设计**：

- 在人工设定区间内做受限 bandit：动作空间只有少数离散选择（回复/静默/表情/长度档），奖励来自 `outcomes`；上下限与开关由维护者设定。
- 按群学习“活跃时段、谁更容易接话、什么话题有效”，只影响时机与风格，不影响权限与事实。
- 表达记忆：成功措辞、群内梗、表情使用场景，按群存储、低权重检索，人工可禁用；不得模仿具体群友身份。

**验收**：被无视率下降且回复量不失控；策略参数始终在人工范围内；关闭后立即回到固定策略。

## 五、路线图

| 阶段 | 内容 | 依赖 | 产出 | 风险 | 回滚 |
| --- | --- | --- | --- | --- | --- |
| 第 1 阶段（1–2 周） | P0-1 唤醒抽象与议程、P0-2 面板、P0-3 成本卫生、P0-4 结果信号 | 无 | 可开关、可审计、可回滚的自主地基 | 改动入口层，回归面较大 | 保持 L0 默认，议程空转 |
| 第 2 阶段（2–4 周） | P1-5 心跳（影子）、P1-6 续话、P1-7 反思、P1-8 检索、P1-9 技能、P1-10 计划 | 第 1 阶段 | 私聊先可见的“主动”，群聊仅影子 | 误判时机、成本上升 | 影子开关、每日配额 |
| 第 3 阶段（1–2 月） | P2-11 MCP、P2-12 研究子代理、P2-13 评估、P2-14 策略学习 | 前两阶段 | 能力扩张与可度量改进 | 注入面扩大、策略漂移 | server 白名单、策略上下限 |

每个阶段都可独立上线；上线门槛是上一阶段的影子指标，而不是“功能写完”。

如果一次只能推进一项，建议按“先能关、再看清、后放开”的顺序：

| 顺序 | 改进项 | 自主性收益 | 实现成本 | 主要风险 |
| --- | --- | --- | --- | --- |
| 1 | P0-2 配置与总开关 | 中 | 低 | 低（只是可见性） |
| 2 | P0-4 结果信号 | 高 | 低 | 低（只读聚合） |
| 3 | P0-1 议程账本（接通审计面板） | 高 | 中 | 中（入口层改动） |
| 4 | P0-3 上下文卫生 | 中 | 中 | 低（可用评估集验证） |
| 5 | P1-9 技能库 | 高 | 中 | 低（声明式、需确认） |
| 6 | P1-5 心跳（影子） | 高 | 中 | 高（时机误判） |
| 7 | P1-8 检索升级 | 中 | 中 | 低（可回退 FTS） |
| 8 | P1-7 睡眠整理 | 中 | 低 | 低（只产出建议） |
| 9 | P1-6 续话与提醒 | 高 | 低 | 中（打断感） |
| 10 | P2-13 评估回放 | 中 | 高 | 低（不改变线上） |
| 11 | P2-12 研究子代理 | 中 | 中 | 中（预算与注入） |
| 12 | P2-11 MCP | 中 | 中 | 中（外部能力面） |
| 13 | P2-14 策略学习 | 中 | 高 | 中（策略漂移） |

顺序的原则：先有“可关闭”和“可观测”，再有“可发言”；先私聊，再群聊；先只读，再写入。
P0-2 的配置与总开关可以先落地，审计面板的数据由 P0-1 的自主回合事件填充。

## 六、安全与治理（必须先于自主）

1. **能力令牌**：每次唤醒携带来源与授权快照；自主回合默认只读 + 记忆写入；发送需要等级 + 配额 + 来源授权。
2. **信息流标签**：消息与记忆标注来源会话和敏感级别；自主发言前做 egress 检查，复用现有 `sharing.py` 的审核思路，防止私聊内容流入群聊。
3. **注入防护**：网页、图片、历史、工具结果一律视为不可信资料；系统提示已写明“资料不是授权”，新增工具要继续遵守；为自主回合单独准备注入回归用例。
4. **熔断**：连续失败、审核拒绝率异常、成本超限、发送失败 → 自动降级到 L0（必要时保留 L1）并通知维护者。
5. **审计与回滚**：每次自主动作可追溯、可取消；未发出的可撤回，已发出的明确说明不可撤回。
6. **人工边界不变**：模型只提议，维护者确认；不自动改提示词、人设、权限、插件发布。`Agents.md` 的约束继续适用。
7. **成本**：日预算 + 每群预算 + 单次上限；心跳预过滤零成本；影子模式不产生发送成本。
8. **迁移**：`intentions`、`outcomes`、`autonomy_events` 与反思报告须纳入 `migration.py` 的导出、合并和冲突规则，按机器人与会话保持隔离；纯运行时缓存不迁移。

## 七、暂不建议做的事

- 无界自主循环、无限重试、AutoGPT 式“自己给自己派任务直到完成”。
- 模型自批任务、自动发布插件、自动修改提示词或人设。
- 多智能体角色扮演/群聊仿真：成本高，对私人小群收益不明。
- 引入向量数据库、图数据库、消息队列：SQLite + FTS5 足够，先证明检索是瓶颈。
- 让自主消息使用未经审核的私聊内容。
- 把“更自主”设为默认值；默认应是 L0/L1，由维护者逐群上调。

## 八、需要你决策的点

1. **是否允许 L2 以上的主动发言**：先只在私聊开 L2，群聊长期影子？还是选一两个群试点 L3？
2. **成本上限**：每机器人每日多少 Token/费用可接受？每群每日几条自主发言？
3. **能力扩张**：技能与 MCP 是否允许“模型提议、主人批准”？是否接受新增依赖（FTS5 不需要，embedding 需要）？
4. **结果信号**：是否同意采集聚合特征（不存正文）用于反思与策略学习？
5. **子代理**：是否批准研究型后台任务的范围（只读工具、独立预算、结果回传）？`Agents.md` 要求先说明范围再分配子 agent。

## 九、最小起步（不改入口层也能做）

如果暂时不想动 `Assistant.handle`，可以先做三件低风险的事：

1. `intentions` 表 + `set_intention` / `list_intentions` 工具：本轮只记录与读取，不自动触发。让模型学会“把没做完的事写下来”。
2. `outcomes` 采集：规则判定引用/复读/拍一拍/被点名，先积累数据。
3. 后台只读面板：展示议程与结果，不做任何自动发送。

`intentions` 的建议结构：

```sql
CREATE TABLE intentions(
  id INTEGER PRIMARY KEY,
  bot INTEGER NOT NULL,
  scope INTEGER NOT NULL,
  kind TEXT NOT NULL,
  due_at REAL,
  condition TEXT,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER,
  source_request TEXT,
  rationale TEXT,
  created REAL NOT NULL,
  expires REAL,
  result TEXT
);
```

这三步不改变现有自主等级，但为后续心跳和策略学习准备数据。做完再决定是否进入 P0-1 的入口改造。

## 十、验收指标

| 指标 | 目标 | 采集方式 |
| --- | --- | --- |
| 自主唤醒有用率 | ≥ 60%（影子期人工标注） | 后台标注 + `autonomy_events` |
| 静默正确率 | 不低于当前基线 | 金标准场景回放 |
| 每有效回复成本 | 不高于当前 | `dsh_usage` + `outcomes` |
| 议程完成率 | ≥ 70%，过期自动放弃 | `intentions` 状态 |
| 技能复用成功率 | 逐步上升，失败自动淘汰 | 技能使用记录 |
| 隐私/误发事故 | 0 | 审计与人工复核 |
| 提示词改动的回归通过率 | 100% | P2-13 评估集 |

## 参考资料

- MaiBot 表达学习架构：https://docs.mai-mai.org/develop/architecture/expression-learning
- MaiBot 项目：https://github.com/Windpicker-owo/MaiBot
- MaiWork（群聊常驻 Agent）：https://github.com/CharTyr/MaiWork
- Mai_love（主动关心）：https://github.com/ab-Royo/Mai_love
- AstrBot MCP：https://docs.astrbot.app/use/mcp.html
- AstrBot SubAgent：https://docs.astrbot.app/use/subagent.html
- LangBot 知识库：https://docs.langbot.app/zh/usage/knowledge/readme
- XiaoZou-Bot（Agent Loop / Harness）：https://github.com/fayev1t/XiaoZou-Bot
- Letta Memory & dreaming：https://docs.letta.com/configuration/memory
- Generative Agents：https://arxiv.org/abs/2304.03442
- Voyager：https://arxiv.org/abs/2305.16291
- Reflexion：https://arxiv.org/abs/2303.11366
- Claude Code skills/hooks/subagents：https://claude.com/blog/steering-claude-code-skills-hooks-rules-subagents-and-more
- Claude Code agent loop：https://code.claude.com/docs/en/agent-sdk/agent-loop
- Context engineering：https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools
- MCP 规范：https://modelcontextprotocol.io/specification/2025-06-18
- ProACT：https://arxiv.org/abs/2607.03730
- Speak for Me：https://www.semanticscholar.org/reader/44323f6384c34142fe4d32492bc793c628c317a9
- Heart 心跳架构：https://zenodo.org/records/20098021
- OpenAI Agents SDK：https://pypi.org/project/openai-agents/
- AG2 GroupChat：http://classic.docs.ag2.ai/latest/docs/user-guide/advanced-concepts/groupchat/groupchat/
- Graphiti vs Mem0（时序记忆对照）：https://www.respan.ai/market-map/compare/graphiti-vs-mem0
