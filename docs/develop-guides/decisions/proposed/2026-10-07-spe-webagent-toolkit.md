# SPE WebAgent 迁移：Python 原生工具面与 Yuxi 平台承接

状态：proposed
类型：feature
Owner：backend/test/unit/toolkits/spe/fixtures/spe_contract_snapshot.json

> Owner 说明：目标代码 Owner 是 `backend/package/yuxi/agents/toolkits/spe`（批次 1 落地后本记录 Owner 改指该路径）；落地前工具面契约的过渡事实由下方冻结快照文件承担。

## 问题

spe-agent 是绑定 dsh CLI 的 SPE Agent 发行版（Node stdio MCP 桥，工具面 47 个工具、其中 14 个写工具走审批闸门、40+ skills、7 个工具模块），Yuxi 平台用户无法使用其能力。三个结构性冲突使"直接接入"不可行：

1. 桥是 stdio MCP server，而 Yuxi 对 MCP 传输有显式白名单（仅 `sse`/`streamable_http`，三处校验拒绝 stdio，并有 `test_mcp_stdio_security.py` E2E 防回归）；
2. 桥的凭据模型是进程级环境变量单用户（`SPE_AGENT_JWT` + 进程内 currentTenantId 缓存），与 Yuxi 多用户平台的 per-request 身份需求冲突；
3. Yuxi 的审批中间件只覆盖 `write_file`/`edit_file`/`execute` 三个沙盒工具，MCP 工具与本地业务工具均不在审批面，SPE 的 14 个写工具必须有人工确认边界。

用户已定方向（2026-10-07）：SPE 能力以 Agent 形态迁入 Yuxi 成为「SPE 运营 WebAgent」，M1 含写操作；SPE 后端零改动、仍是业务事实源（延续 S2 绞杀者原则）；浏览器自动化能力域另行决策（本记录不含）。dsh 发行版冻结（只修缺陷），Yuxi 成为唯一活跃宿主；工具面契约的事实源随迁移搬入 Yuxi。

## 提案

在 `backend/package/yuxi/agents/toolkits/spe` 用 Python 原生重写 SPE 工具面，进程内直连 SPE REST API。批次推进（M1 蓝图）：批次 0 冻结契约快照为移植 oracle → 批次 1 toolkit 骨架（HTTP client、per-request 凭据解析、错误码体系、幂等回执）→ 批次 2 只读工具面移植（含 yuxi 三工具内化）→ 批次 3 写通道与审批扩展 → 批次 4 skills 资产与 Agent 模板 → 批次 5 呈现最小集与 staging 真机验收。

### 实现方案

- **工具面**：`toolkits/spe` 子包承载全部工具；本地 `@tool` 注册（`toolkits/registry.py` 机制），Skill 门控走本地工具的"预注册隐藏"形态以延续 TOOL_MODULES 模块语义；工具执行在 ARQ worker 进程内发出 HTTP 调用，`handle_tool_error` 语义与既有 toolkit 一致。
- **出站契约（C-11/C-12 已随批次 1 落地）**：统一工具端点 `POST {base}/api/v1/agent/tools/{tool}`（`X-Agent-Token` 服务令牌 + body `sessionId`/`args`，120s 超时），会话创建 `POST {base}/api/v1/agent/sessions`（Bearer 用户 JWT，id 提取 sessionId→id→data.id）；`client.py` 拥有出站与错误归一，`sessions.py` 拥有按 uid 键控的进程内会话注册表（并发共享创建、失败不污染、条件失效），`errors.py` 拥有 22+6 错误码与模型降级 guidance（对快照逐字移植，四条 dsh 语境显式适配）。
- **身份**：工具经 langgraph `ToolRuntime.context.uid` 解析当前用户的 SPE 凭据（先例：`toolkits/kbs/tools.py` 工具体内可见性过滤）。凭据两层已定稿并落地：系统级连接（base_url + X-Agent-Token 服务令牌）进 `config/options.py` 的 `spe_agent_opts`（service_token 敏感项，env 回落 `SPE_AGENT_*`）；用户级 JWT 存 `user_spe_credentials` 表（business schema v10，`upgrade_user_spe_credentials` 迁移步骤，uid 唯一、FK 级联删除、明文列循 ModelProvider 先例），`login`/`logout` 工具是唯一写入口并联动会话注册表失效；读取经 `repositories/spe_credential_repository`（自开会话，永不回显全文）。
- **契约对账**：批次 0 把 spe-agent 桥源码的工具面（名称、参数 schema、读/写、模块归属、卡片类型、错误码与 guidance）提取为 Yuxi 侧冻结快照（`backend/test/unit/toolkits/spe/fixtures/`），并钉死来源 commit；批次 2/3 每个工具的移植以快照为 oracle 写对账测试。spe-agent 契约文档冻结为历史参照，不再回写。
- **写通道（批次 3A 已落地）**：`write_catalog` 冻结 biz-post 10 工具（8 闸门写 + 2 预览）与 14 名闸门名单（`contract_names.py` 纯数据，审批层单一来源）；`receipts.py` 拥有 C-26 写回执（120s 窗、opKey=sha256(uid+tool+稳定串)、op_{tool}_{seq}、duplicate 回吐首执回执+逐字句、失败不缓存、在飞共享一次执行）；`write_tools.py` 按 T04 口径执行（审批 mode 驱动、`interrupt_on` 无谓词全参数确认、SubAgent 隐藏经 SENSITIVE_BACKEND_TOOLS 扩员）；`call_spe_biz_mutate` 承载 C-26 错误归一（400/402 特判、2xx 空 body 宽容、generate_listing 300s 覆写）。
- **Skills 与 Agent 暴露（批次 4 已落地）**：T06 定案——共享 Skill 包（zip → import/prepare → admin confirm 全局 read_scope，个人 skill 的 tool_dependencies 恒空故必须走共享）、M1 不建新 preset（chatbot `context.skills="all"` + SkillsMiddleware 默认装配）。已安装 5 个：spe-account（新增身份管理）、knowledge-center、product-selection-workflow、source-listing-workflow、ozon-selection-agent（提示词逐字，render_ui/navigate_to 引用做降级适配并标注），26 项工具依赖对账全部命中注册表；智能助手 Agent 配置 `preload_skills` 首轮即用。nav 30 skills 随批次 5。
- **审批**：`tool_approval` 的覆盖面从三个沙盒工具扩展到 spe 写工具（扩展谓词与档位由写操作审批决策定稿），保持"写权力在人工确认不在模型"。
- **biz 读工具面（批次 2A 已落地）**：`catalog.py` 持有 16 个纯 SPE 读工具的冻结参数面（name/description/参数/卡面逐字对齐快照，对账测试守护）与端点映射；`biz.py` 按每请求管线执行（uid → JWT fail-closed → base_url → 分 kind query 组装 → `call_spe_biz_get`）；`resolvers.py` 拥有租户/店铺辅助解析（/auth/me、stores 列表，每次现解析不缓存）。navigate_to/render_ui 两个本地卡面工具依赖卡片呈现决策（T05），顺延至批次 5，无 consumer 不上线。
- **agent-session 工具面（批次 2B 已落地）**：`catalog.py` 增 6 工具冻结规格（search_products/keyword_hot_words/competitor_analysis/store_metrics/score_candidates/generate_report）；`agent_tools.py` 持有 C-14 会话管线——uid 会话键 ensure → 统一 POST 端点，SESSION_NOT_FOUND/SESSION_NOT_ACTIVE 条件失效重建后**仅重试一次**，其余错误不重试；后端 UiBlock 信封原样透传（blockId 由 SPE 后端拥有）；计费/预算闸在后端带内透传，客户端不设闸。
- **yuxi 三工具内化（读面批次 2C 已落地）**：`kb_tools.py` 直调 `KnowledgeBaseManager`——scope 三态坍缩为 uid 可见集（映射保留：official=read_scope global，tenant=其余；category 兼容保留恒忽略）；I-1 零探测（不可见 kbId 不触任何检索）、I-3 单库失败降级全败才报、M-4 原始分排序缺失垫 0、kb_read 两窗 3600 行 + M-1 行号剥除；YUXI_NOT_CONFIGURED/AUTH_FAILED 错误族退役。`kb_upload`（upload 抽 service + 授权模型显式决策）与 `yuxi_research`（直调 submit_agent_request）属批次 3 写通道；`permission_console` 的数据 Owner（租户权限面）尚不存在且卡面依赖 render_ui，一并顺延批次 3。
- **凭据与出站纪律**：延续 C-22（出站 body/URL 恒无 tenantId，身份只体现在凭据选择）与 I-1（跨租户目标零出站 fail-closed）。

## 替代方案

- **MCP 层新增 stdio 传输直接宿 Node 桥**：需推翻 Yuxi 有意的安全决策（传输白名单三处校验 + E2E 防回归测试），并在 worker 内管理 Node 子进程生命周期（spawn/重启/泄漏），新增长期维护面。拒绝。
- **HTTP MCP 网关容器（supergateway/mcp-proxy 类）包桥**：TS 资产 100% 复用、Yuxi 零核心改动；但新增一个 Compose 服务，桥的进程级单用户 env 认证仍必须先改造出 per-request 身份层（改造后桥退化为纯协议转换层），且 MCP server 全局可见、凭据 headers 明文存库与 per-user 凭据语义不贴。拒绝；若 dsh 发行版未来解冻需并行维护，可重估。
- **延续 dsh 宿主不迁移**：与用户已定方向（Yuxi 唯一活跃宿主）相悖。
- **SPE 业务功能域整体移植进 Yuxi**（商品/订单/库存/定价重写为 Yuxi 域）：与 Yuxi 知识库/智能体平台定位冲突，用户已明确出范围。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 契约快照与桥源码一致（47 工具、14 写、模块归属） | 快照漂移或计数与 s2t 契约不符 | `backend/test/unit/toolkits/spe/fixtures/` 快照 + 对账断言 | `docker compose exec api uv run --group test --no-sync pytest test/unit/toolkits/spe -m "not slow"` 快照不变量测试（本机容器需 `--no-sync`，见 PR 说明） | 人为增删快照工具行，计数断言变红 | Passed |
| Yuxi 用户对话取数回执含真实 SPE staging 数据 | 工具面/认证/取数链路断裂 | `toolkits/spe` 工具 + SPE 后端 | 批次 5 E2E：staging 真机全链路 | 凭据缺失/过期时结构化报错而非静默空结果 | Not run |
| 14 写工具调用必经人工审批，confirm 回执语义保留 | 审批覆盖缺口或回执幂等丢失 | `tool_approval` 扩展 + spe 写回执 | integration：审批放行/拒绝两路 + 幂等重放断言 | 撤掉审批谓词，写工具直接执行 → 测试变红 | Not run |
| per-request 身份：不同 uid 解析各自凭据，跨租户目标零出站 | 身份串号或探测出站 | spe 凭据解析层 | unit（假后端）+ integration：双 uid 各自凭据断言 | uid 无凭据时调用 → fail-closed 错误，零 HTTP 出站 | Not run |
| yuxi 三工具内化后行为与 s2t 契约等价 | 内化路径行为漂移 | 内化工具 + 契约 oracle 对账测试 | unit：假后端契约对账（信封形状、降级、截断语义） | 改动任一信封字段 → 对账测试变红 | Not run |
| Skill 激活才开放对应模块工具 | 门控失效工具全量可见 | `SkillsMiddleware` 本地 gated 机制 | unit：未激活 Skill 时工具对模型不可见 | 移除门控声明 → 可见性断言变红 | Not run |

## 风险

- 审批扩展触碰全局中间件装配，谓词与档位设计不当会放大所有 Agent 的审批噪音（由写操作审批决策约束）。
- `kb_upload` 抽 upload service 触碰知识域"路由薄、service 拥有用例"边界，需独立 review。
- 47 工具一次性移植的回归面大：依赖批次 0 快照对账逐工具兜底，移植顺序按批次 2（读）→ 3（写）控制风险。
- SPE staging 凭据（service token / 用户 JWT）发放节奏是批末真机验收的外部依赖；不到位时该批验收记 Not run。
- dsh 冻结是单向决定：若未来需要回滚双宿主，需重建双源同步机制（Q4 已把契约事实源搬入 Yuxi）。
- 浏览器自动化轨（部署级探针）不在本记录范围，其沙盒/凭据风险由后续独立决策承担。
