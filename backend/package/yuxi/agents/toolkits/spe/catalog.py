"""SPE biz 读工具目录（biz-get 通道 16 工具）。

参数面与 description 逐字冻结自契约快照（backend/test/unit/toolkits/spe/fixtures/
spe_contract_snapshot.json，源 spe-agent commit 9225d3ec）；端点与 query 组装
规则来自桥 index.ts/biz.ts（提取参照 docs/vibe/wayfinder-spe-webagent/assets/
spe-biz-endpoints.md）。kb_list/kb_read/yuxi_kb_search 不在本目录——它们按迁移
决策内化为 Yuxi 进程内直调（批次 2C）。对账测试保证本目录与快照一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BizReadSpec:
    """一个工具的完整定义：模型面 + 出站面；card 为卡面类型（快照 card_type）。"""

    name: str
    description: str
    path: str
    kind: str
    card: str = ""
    params: list[dict] = field(default_factory=list)


SPE_BIZ_READ_TOOLS: list[BizReadSpec] = [
    BizReadSpec(
        name="list_orders",
        description="查询本租户订单列表，支持状态/关键词/店铺/日期区间过滤与分页；status 传英文枚举",
        path="/api/v1/orders",
        kind="list_orders",
        params=[
            {
                "name": "status",
                "type": "string",
                "required": False,
                "description": "英文枚举如 delivered/delivering/awaiting_packaging/awaiting_deliver/cancelled",
            },
            {"name": "keyword", "type": "string", "required": False, "description": "≤100 字符，越界截断不报错"},
            {
                "name": "store",
                "type": "string",
                "required": False,
                "description": "店铺名或店铺 ID（list_stores 可查），不传=全部店铺",
            },
            {"name": "startDate", "type": "string", "required": False, "description": "YYYY-MM-DD"},
            {"name": "endDate", "type": "string", "required": False, "description": "YYYY-MM-DD"},
            {"name": "page", "type": "integer", "required": False, "description": "≥0，缺省 0，负数与非整数按缺省"},
            {"name": "size", "type": "integer", "required": False, "description": "1–100，缺省 20，越界夹紧不报错"},
        ],
    ),
    BizReadSpec(
        name="inventory_health",
        description="获取本租户全部商品库存健康报告，按商品逐行",
        path="/api/v1/inventory/management/health-report",
        kind="plain",
        params=[],
    ),
    BizReadSpec(
        name="hot_products",
        description="查询选品广场热销榜（按周期同步自 Ozon 官方）；恒按榜单名次升序；需 ADMIN 角色",
        path="/api/v1/admin/ozon-what-to-sell/products",
        kind="hot_products",
        params=[
            {"name": "keyword", "type": "string", "required": False, "description": "关键词过滤"},
            {"name": "category1", "type": "string", "required": False, "description": "一级类目过滤"},
            {"name": "brand", "type": "string", "required": False, "description": "品牌过滤"},
            {"name": "minPrice", "type": "number", "required": False, "description": "价格下限"},
            {"name": "maxPrice", "type": "number", "required": False, "description": "价格上限"},
            {"name": "period", "type": "string", "required": False, "description": "enum weekly|monthly，缺省 weekly"},
            {"name": "page", "type": "integer", "required": False, "description": "≥0，缺省 0"},
            {"name": "size", "type": "integer", "required": False, "description": "1–100，缺省 20"},
        ],
    ),
    BizReadSpec(
        name="monthly_report",
        description="获取指定年月店铺经营月报；店铺缺省取首个启用店铺",
        path="/api/v1/enhanced-reports/monthly",
        kind="monthly_report",
        params=[
            {"name": "year", "type": "integer", "required": True, "description": "年份如 2026"},
            {"name": "month", "type": "integer", "required": True, "description": "1–12"},
            {"name": "store", "type": "string", "required": False, "description": "店铺名或 ID，不传=首个启用店铺"},
        ],
    ),
    BizReadSpec(
        name="source_products",
        description="浏览采集箱源货列表，按更新时间倒序分页",
        path="/api/v1/source-products",
        kind="paged",
        params=[
            {"name": "page", "type": "integer", "required": False, "description": "≥0，缺省 0"},
            {"name": "size", "type": "integer", "required": False, "description": "1–100，缺省 20"},
        ],
    ),
    BizReadSpec(
        name="list_stores",
        description="查看本租户 Ozon 店铺列表；供其他工具 store 参数取店铺名或 ID",
        path="/api/v1/integration/ozon/stores",
        kind="plain",
        params=[],
    ),
    BizReadSpec(
        name="credit_transactions",
        description="查询全平台积分流水明细（9 列），支持类型过滤与时间范围，按创建时间倒序；需 SUPER_ADMIN 角色",
        path="/api/v1/credit/admin/transactions",
        kind="credit_transactions",
        params=[
            {"name": "page", "type": "integer", "required": False, "description": "≥0，缺省 0"},
            {"name": "size", "type": "integer", "required": False, "description": "1–100，缺省 20"},
            {"name": "type", "type": "string", "required": False, "description": "如 consume/recharge/admin_adjust"},
            {"name": "dateRange", "type": "string", "required": False, "description": "enum 7d|30d|all，缺省 all"},
        ],
    ),
    BizReadSpec(
        name="dashboard_stats",
        description="获取数据看板统计（经营指标/商品健康）；可选 store 筛选",
        path="/api/v1/dashboard/stats",
        kind="dashboard_stats",
        params=[{"name": "store", "type": "string", "required": False, "description": "店铺名或 ID，不传=全部店铺"}],
    ),
    BizReadSpec(
        name="list_products",
        description="查询本租户商品列表；tenantId 由桥按登录态解析注入，不进工具参数",
        path="/api/v1/products",
        kind="tenant_scoped",
        params=[],
    ),
    BizReadSpec(
        name="sales_summary",
        description="获取销售分析总览，支持日期区间过滤，缺省取最新采集周期",
        path="/api/v1/ozon/analytics/summary",
        kind="date_range",
        params=[
            {"name": "dateFrom", "type": "string", "required": False, "description": "YYYY-MM-DD"},
            {"name": "dateTo", "type": "string", "required": False, "description": "YYYY-MM-DD"},
        ],
    ),
    BizReadSpec(
        name="sales_products",
        description="按商品的销售分析，支持日期区间与分页，按订单件数降序",
        path="/api/v1/ozon/analytics/products",
        kind="date_range_paged",
        params=[
            {"name": "dateFrom", "type": "string", "required": False, "description": "YYYY-MM-DD"},
            {"name": "dateTo", "type": "string", "required": False, "description": "YYYY-MM-DD"},
            {"name": "page", "type": "integer", "required": False, "description": "≥0，缺省 0"},
            {"name": "size", "type": "integer", "required": False, "description": "1–100，缺省 20"},
        ],
    ),
    BizReadSpec(
        name="low_stock",
        description="获取本租户低库存项列表；tenantId 桥内解析注入",
        path="/api/v1/inventory/low-stock",
        kind="tenant_scoped",
        params=[],
    ),
    BizReadSpec(
        name="replenishment",
        description="获取本租户补货建议列表",
        path="/api/v1/inventory/management/replenishment/recommendations",
        kind="plain",
        params=[],
    ),
    BizReadSpec(
        name="ops_plans",
        description="查询运营计划列表，支持状态过滤（缺省 ACTIVE）与分页",
        path="/api/v1/opsplan/plans",
        kind="ops_plans",
        params=[
            {
                "name": "status",
                "type": "string",
                "required": False,
                "description": "enum PREPARING|ACTIVE|ARCHIVED|FAILED，缺省 ACTIVE，枚举外值按缺省",
            },
            {"name": "page", "type": "integer", "required": False, "description": "≥0，缺省 0"},
            {"name": "size", "type": "integer", "required": False, "description": "1–100，缺省 20"},
        ],
    ),
    BizReadSpec(
        name="get_source_draft",
        description="查看采集箱源货的 Ozon 上架草稿状态（含生图状态）；generate_ozon_images 启动后用本工具轮询进度",
        path="/api/v1/source-products/{id}/draft",
        kind="source_draft",
        params=[
            {"name": "sourceProductId", "type": "integer", "required": True, "description": "采集箱源货 ID，整数 ≥1"}
        ],
    ),
    BizReadSpec(
        name="collection_status",
        description="查看采集管道状态（采集统计/管道活性/最近 5 条）；插件与工作台写同一后端管道，为桥接判据卡",
        path="/api/v1/products/collected/stats",
        kind="collection_status",
        params=[],
    ),
]

# 卡面类型（快照 card_type 逐字）：blockId 引用与批次 5 卡面渲染依赖
BIZ_CARD_TYPES: dict[str, str] = {
    "list_orders": "panel",
    "inventory_health": "data-table",
    "hot_products": "product-table",
    "monthly_report": "score-report",
    "source_products": "data-table",
    "list_stores": "panel",
    "credit_transactions": "panel",
    "dashboard_stats": "score-report",
    "list_products": "panel",
    "sales_summary": "score-report",
    "sales_products": "panel",
    "low_stock": "panel",
    "replenishment": "panel",
    "ops_plans": "panel",
    "get_source_draft": "panel",
    "collection_status": "score-report",
}


# agent-session 通道 6 工具（统一 POST 端点 + SPE 会话，快照逐字）。
# 出站走 client.call_spe_tool；会话失效（SESSION_NOT_FOUND/SESSION_NOT_ACTIVE）
# 由执行层条件失效重建后仅重试一次（契约 C-14）。
SPE_AGENT_SESSION_TOOLS: list[BizReadSpec] = [
    BizReadSpec(
        name="search_products",
        description="检索 Ozon 平台热销榜商品（最多 50 条），按关键词/类目/品牌/价格区间过滤",
        path="/api/v1/agent/tools/{tool}",
        kind="agent_session",
        card="product-table",
        params=[
            {"name": "keyword", "type": "string", "required": False, "description": "≤120 字符，模糊匹配名称/SKU/卖家"},
            {"name": "category", "type": "string", "required": False, "description": "≤60 字符，一级类目名"},
            {"name": "brand", "type": "string", "required": False, "description": "≤60 字符"},
            {"name": "minPrice", "type": "number", "required": False, "description": "均价下限（₽）"},
            {"name": "maxPrice", "type": "number", "required": False, "description": "均价上限（₽）"},
            {"name": "period", "type": "string", "required": False, "description": "enum weekly|monthly，缺省 weekly"},
            {"name": "limit", "type": "integer", "required": False, "description": "1–50，缺省 20，越界夹紧不报错"},
            {
                "name": "sort",
                "type": "object",
                "required": False,
                "description": "{field: enum sales30|priceRuble|title|skuId, dir: enum asc|desc}，additionalProperties:false，缺省 sales30 降序，未知字段退默认不报错",  # noqa: E501
            },
        ],
    ),
    BizReadSpec(
        name="keyword_hot_words",
        description="查询 Ozon 卖家后台实时搜索热词榜；按次计费、每租户每分钟预算闸门，被预算拒绝时不要重试",
        path="/api/v1/agent/tools/{tool}",
        kind="agent_session",
        card="trend-chart",
        params=[
            {"name": "text", "type": "string", "required": False, "description": "词根，≤100 字符，可空=大盘热词"},
            {"name": "limit", "type": "integer", "required": False, "description": "1–50，缺省 20"},
            {"name": "period", "type": "string", "required": False, "description": "enum days_7|days_30，缺省 days_7"},
        ],
    ),
    BizReadSpec(
        name="competitor_analysis",
        description="对一组 Ozon SKU 做竞品体检（价格/销量/评分均值）；24h 未命中缓存走上游采集按次计费",
        path="/api/v1/agent/tools/{tool}",
        kind="agent_session",
        card="product-table",
        params=[
            {"name": "skus", "type": "array", "required": True, "description": "string 数组，必填最多 20 个"},
            {"name": "sort", "type": "object", "required": False, "description": "同 search_products.sort"},
        ],
    ),
    BizReadSpec(
        name="store_metrics",
        description="本店铺近 N 天订单量与销售额日趋势（日期轴逐日补齐）",
        path="/api/v1/agent/tools/{tool}",
        kind="agent_session",
        card="trend-chart",
        params=[{"name": "days", "type": "integer", "required": False, "description": "1–90，缺省 30"}],
    ),
    BizReadSpec(
        name="score_candidates",
        description="对候选 SKU 取 Jev 出品打分；双闸（jev.enabled/jev.exposure-enabled）关时带内 JEV_DISABLED；带内错误不重试；分数不参与排序",  # noqa: E501
        path="/api/v1/agent/tools/{tool}",
        kind="agent_session",
        card="product-table",
        params=[
            {
                "name": "skus",
                "type": "array",
                "required": True,
                "description": "必填非空最多 20 个，逐个 ≤64 字符；特征由后端按会话反查",
            }
        ],
    ),
    BizReadSpec(
        name="generate_report",
        description="把本轮已取数据汇编成选品报告（标题+小节）；统计值只认后端从已下发数据块现算；只读不计费；sections 缺失被参数校验挡下、空数组 INVALID_ARGS(400)",  # noqa: E501
        path="/api/v1/agent/tools/{tool}",
        kind="agent_session",
        card="score-report",
        params=[
            {"name": "title", "type": "string", "required": False, "description": "≤200 字符，缺省后端给定"},
            {
                "name": "sections",
                "type": "array",
                "required": True,
                "description": "必填非空最多 20 节；每节 heading 必填 ≤200（缺标题整节丢弃）、bodyMarkdown ≤4000 超限裁剪、lists ≤30 条×≤120 字符、blockId=回执摘要行 blockId=blk_… 后那一串；additionalProperties:true（多写键后端丢弃）",  # noqa: E501
            },
        ],
    ),
]


# 内化知识面 3 工具（kb_list/kb_read/yuxi_kb_search）：原桥 HTTP 外呼 Yuxi，
# 迁移后进程内直调 KnowledgeBaseManager（参数面仍逐字冻结；scope 语义映射
# 见 kb_tools.py：official=read_scope global，tenant=其余；category 参数兼容保留）。
SPE_INTERNALIZED_KB_TOOLS: list[BizReadSpec] = [
    BizReadSpec(
        name="kb_list",
        description="浏览知识中心文章列表；双 scope（official 全租户共享/tenant 本租户私有），scope 缺省 all 合并",
        path="",
        kind="internal_kb",
        card="panel",
        params=[
            {"name": "scope", "type": "string", "required": False, "description": "enum all|official|tenant，缺省 all"},
            {
                "name": "category",
                "type": "string",
                "required": False,
                "description": "分类过滤如 选品方法/运营SOP/平台帮助",
            },
            {"name": "q", "type": "string", "required": False, "description": "标题关键词过滤"},
            {"name": "page", "type": "integer", "required": False, "description": "≥0，缺省 0"},
            {"name": "size", "type": "integer", "required": False, "description": "1–100，缺省 20"},
        ],
    ),
    BizReadSpec(
        name="kb_read",
        description="阅读知识中心单篇文章正文；记录ID 从 kb_list 面板获取；跨租户文章服务端 404",
        path="",
        kind="internal_kb",
        card="article",
        params=[
            {
                "name": "id",
                "type": "string",
                "required": True,
                "description": "必填 ≤128 字符，须为「{kb_id}/{file_id}」复合记录ID",
            }
        ],
    ),
    BizReadSpec(
        name="yuxi_kb_search",
        description="语义检索知识中心（RAG 向量+关键词混合）；官方与租户 KB 并发合并按分数排序；需 Yuxi 后端，未配置报 YUXI_NOT_CONFIGURED",  # noqa: E501
        path="",
        kind="internal_kb",
        card="panel",
        params=[
            {"name": "query", "type": "string", "required": True, "description": "≤200 字符，越界截断不报错"},
            {"name": "scope", "type": "string", "required": False, "description": "enum all|official|tenant，缺省 all"},
            {"name": "file_name", "type": "string", "required": False, "description": "片段按来源文件名过滤"},
            {"name": "top_k", "type": "integer", "required": False, "description": "1–20，缺省 10，越界夹紧"},
        ],
    ),
]


# biz-post 写通道 10 工具（8 闸门写 + 2 预览；kb_upload/yuxi_research 属 yuxi 通道，
# 内化批次 3B）。参数面与 description 逐字冻结自快照；端点与 query/body 映射
# 见 write_tools.py（源桥 index.ts 写分支）。
SPE_BIZ_WRITE_TOOLS: list[BizReadSpec] = [
    BizReadSpec(
        name="preview_dynamic_price",
        description="预览指定商品动态定价推荐，纯预览不改价不落库；不进写审批闸门、不做幂等去重（预览类）",
        path="",
        kind="biz_preview",
        card="data-table",
        params=[{"name": "productId", "type": "string", "required": True, "description": "maxLength 64"}],
    ),
    BizReadSpec(
        name="apply_dynamic_price",
        description="应用指定价格记录的动态定价推荐，应用后新价格即生效；写回执「已应用动态推荐价（价格记录 #<id>）」",
        path="",
        kind="biz_write",
        card="confirm-result",
        params=[{"name": "priceId", "type": "integer", "required": True, "description": "价格记录 ID"}],
    ),
    BizReadSpec(
        name="restock_inventory",
        description="为指定库存项补货；写回执「已补货库存项 #<id>：+<数量> 件 @ <单价>」",
        path="",
        kind="biz_write",
        card="confirm-result",
        params=[
            {"name": "inventoryId", "type": "integer", "required": True, "description": "库存项 ID"},
            {"name": "quantity", "type": "integer", "required": True, "description": "正整数"},
            {"name": "unitCost", "type": "number", "required": True, "description": "单位成本（数字）"},
        ],
    ),
    BizReadSpec(
        name="gift_credits",
        description="向指定租户赠送积分调账；需 SuperAdmin 角色；写回执「已向租户 <t> 赠送 <a> 积分，余额 <b>」",
        path="",
        kind="biz_write",
        card="confirm-result",
        params=[
            {"name": "tenantId", "type": "integer", "required": True, "description": "租户 ID，整数 ≥1"},
            {"name": "amount", "type": "integer", "required": True, "description": "赠送积分数量，整数 ≥1"},
            {
                "name": "remark",
                "type": "string",
                "required": False,
                "description": "≤200 字符，超长拒绝；opKey/body 中 remark 缺省不带键",
            },
        ],
    ),
    BizReadSpec(
        name="collect_products",
        description="按 SKU 采集 Ozon 商品选品数据（第三方源本地缓存 1 天，记入采集历史）；预览类：不进写审批闸门、不做幂等去重",  # noqa: E501
        path="",
        kind="biz_preview",
        card="panel",
        params=[{"name": "skus", "type": "array", "required": True, "description": "1–50 个非空字符串（maxItems 50）"}],
    ),
    BizReadSpec(
        name="build_rich_content",
        description="为源货草稿生成/重新生成 Ozon Rich Content 富内容（图文 JSON 写回草稿；按次扣积分）",
        path="",
        kind="biz_write",
        card="confirm-result",
        params=[
            {"name": "sourceProductId", "type": "integer", "required": True, "description": "采集箱源货 ID，整数 ≥1"}
        ],
    ),
    BizReadSpec(
        name="generate_variant_titles",
        description="AI 为源货每个 SKU 规格生成俄语变体标题并写回（按次扣积分，AI 失败自动回退拼接）",
        path="",
        kind="biz_write",
        card="confirm-result",
        params=[
            {"name": "sourceProductId", "type": "integer", "required": True, "description": "采集箱源货 ID，整数 ≥1"}
        ],
    ),
    BizReadSpec(
        name="host_source_images",
        description="把源货 1688 图片托管到 SPE 公网图床（Ozon 上架拉图前置步骤）",
        path="",
        kind="biz_write",
        card="confirm-result",
        params=[
            {"name": "sourceProductId", "type": "integer", "required": True, "description": "采集箱源货 ID，整数 ≥1"}
        ],
    ),
    BizReadSpec(
        name="generate_ozon_images",
        description="AI 生成 Ozon 套图（异步启动，ZenMux 引擎，49 积分/张×参考图系数）；启动后用 get_source_draft 轮询",
        path="",
        kind="biz_write",
        card="confirm-result",
        params=[
            {"name": "sourceProductId", "type": "integer", "required": True, "description": "采集箱源货 ID，整数 ≥1"},
            {
                "name": "count",
                "type": "integer",
                "required": False,
                "description": "生成张数，缺省按后端模板；opKey 中记 count ?? null",
            },
        ],
    ),
    BizReadSpec(
        name="generate_listing",
        description="为采集箱源货一键生成 Ozon 上架草稿（AI 同步 1–3 分钟，timeoutMs 300s 覆写缺省 120s；按次扣积分，402→BIZ_INSUFFICIENT_CREDIT）",  # noqa: E501
        path="",
        kind="biz_write",
        card="confirm-result",
        params=[
            {
                "name": "sourceProductId",
                "type": "integer",
                "required": True,
                "description": "源货 ID（source_products 列表 id），整数 ≥1",
            },
            {
                "name": "store",
                "type": "string",
                "required": False,
                "description": "店铺名或 ID，不传=首个启用店铺；opKey 按参数面去重（store 名与缺省首店 opKey 不同）",
            },
        ],
    ),
]


# 内化写 2 工具（kb_upload / yuxi_research，原桥 yuxi 通道 HTTP 外呼）。
# kb_upload 参数面做文档化适配：scope→kb_id（spe-yuxi.json 映射随 M-2 配置
# 退役，目标知识库由调用方显式指定）；其余逐字冻结。
SPE_INTERNAL_WRITE_TOOLS: list[BizReadSpec] = [
    BizReadSpec(
        name="kb_upload",
        description="上传知识文件到指定知识库：上传→登记→解析→轮询→入库五步链；同内容去重；解析超时不报错、回执注明索引未触发；需管理员且对目标库有管理权限。",  # noqa: E501
        path="",
        kind="internal_write",
        card="confirm-result",
        params=[
            {
                "name": "kb_id",
                "type": "string",
                "required": True,
                "description": "目标知识库 ID（kb_list 面板的记录ID 前半段）",
            },  # noqa: E501
            {
                "name": "filename",
                "type": "string",
                "required": True,
                "description": "文件名，.md/.txt 结尾、不含路径分隔符、≤200 字符",
            },  # noqa: E501
            {
                "name": "content",
                "type": "string",
                "required": True,
                "description": "markdown 文本，≤1MB（1,000,000 字符）",
            },  # noqa: E501
        ],
    ),
    BizReadSpec(
        name="yuxi_research",
        description="调用 Yuxi deep-research 深度研究智能体（多步检索产出研究回答；写级算力消耗）；agent-call 两端点 runs/result，轮询上限 240s，超时返回 run_id 可续取；无条件注册+运行时守卫",  # noqa: E501
        path="",
        kind="internal_write",
        card="confirm-result",
        params=[
            {
                "name": "question",
                "type": "string",
                "required": True,
                "description": "≤2000 字符（maxLength 2000），越界截断不报错；非空校验含纯空白",
            }
        ],
    ),
]

# 订单状态机写工具（P1#06 扩展面：冻结快照 47 工具之外，ozon-agent 自有规格）。
# 对齐 SPE OrderController：POST /api/v1/orders/{id}/{confirm|ship|cancel|refund}。
SPE_ORDER_WRITE_TOOLS: list[BizReadSpec] = [
    BizReadSpec(
        name="confirm_order",
        description="确认待处理订单；写回执「订单 #<id> 已确认」",
        path="",
        kind="biz_write",
        card="confirm-result",
        params=[
            {"name": "orderId", "type": "integer", "required": True, "description": "订单 ID（正整数）"},
        ],
    ),
    BizReadSpec(
        name="ship_order",
        description="订单发货（必填运单号与承运商）；写回执「订单 #<id> 已发货（<承运商> <运单号>）」",
        path="",
        kind="biz_write",
        card="confirm-result",
        params=[
            {"name": "orderId", "type": "integer", "required": True, "description": "订单 ID（正整数）"},
            {"name": "trackingNumber", "type": "string", "required": True, "description": "运单号（1-100 字符）"},
            {"name": "carrier", "type": "string", "required": True, "description": "承运商（1-50 字符，如 CDEK/Boxberry）"},
        ],
    ),
    BizReadSpec(
        name="cancel_order",
        description="取消订单（必填原因）；写回执「订单 #<id> 已取消：<原因>」",
        path="",
        kind="biz_write",
        card="confirm-result",
        params=[
            {"name": "orderId", "type": "integer", "required": True, "description": "订单 ID（正整数）"},
            {"name": "reason", "type": "string", "required": True, "description": "取消原因（1-200 字符）"},
        ],
    ),
    BizReadSpec(
        name="refund_order",
        description="处理订单退款（必填原因）；写回执「订单 #<id> 已退款：<原因>」",
        path="",
        kind="biz_write",
        card="confirm-result",
        params=[
            {"name": "orderId", "type": "integer", "required": True, "description": "订单 ID（正整数）"},
            {"name": "reason", "type": "string", "required": True, "description": "退款原因（1-200 字符）"},
        ],
    ),
]
