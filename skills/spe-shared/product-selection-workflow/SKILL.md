---
name: product-selection-workflow
tool_dependencies:
  - collection_status
  - collect_products
  - source_products
  - hot_products
  - sales_products
  - sales_summary
  - list_stores
  - generate_listing
description: 选品工作流编排——Chrome 插件（鸥选速麦）圈候选 × 工作台取数出卡。触发词：选品、帮我选品、找爆款、圈候选、SKU 对比、竞品对比、筛选热卖。当用户想做商品筛选/对比/评估，或提到用浏览器插件采集时使用本 skill；单纯查订单或看财务报表不要用本 skill。
---
# 选品工作流（Chrome 插件 × 工作台协作）

分工原则：用户在 Chrome 插件（鸥选速麦）里逛 Ozon 圈候选，采集数据自动写入 SPE 后端；工作台负责取数、对比、可视化出卡、生成上架文案。两者通过 SPE 后端共享数据——**工作台不直接驱动插件**，也不要建议用户"去点插件按钮"（那是用户的操作）。

## 标准流程

### ① 确认采集管道
调 `collection_status`（无参数）：最新采集时间是几分钟前 = 插件侧采集正常；若「管道沉寂」（超 24 小时无新采集），提醒用户二选一：
- 在 Chrome 插件里采几件候选品，采完回来重新看 `collection_status`
- 或直接把候选 SKU 列表发给你，由工作台侧采集（见②）

### ② 取候选数据（按用户给的东西二选一）
- **用户给了 SKU 列表**（1–50 个）→ 调 `collect_products`，参数 skus 为字符串数组。返回选品面板卡（销量/均价/加购转化/广告占比/主图/链接）。
- **用户没给 SKU** → 调 `source_products` 看采集箱里已有的货；采集箱也空 → 用 `hot_products` 拉 Ozon 官方热销榜圈候选（需要 ADMIN 角色）。

### ③ 对比出卡（多品时）
数据齐了以后用紧凑 Markdown 表格做对比（列：品名/销量/均价/加购转化/结论），结论列给「推荐/观望」并注明数据口径（卡片式对比界面随后续批次开放）。
- 单品深看：采集字段本身够用；销量趋势可再用 `sales_products` / `sales_summary` 交叉（走销售分析库，需有采集数据）。

### ④ 选定品 → 生成上架草稿
用户选定某个品后，问一句要不要生成草稿；确认后调 `generate_listing`（sourceProductId 必填；store 可传店铺名，可用 `list_stores` 查）。这是**写操作**：会弹人工审批卡，同步加工约 1–3 分钟，按次扣积分（积分不足会明确报错，引导充值，不要重试）。

## 纪律

- **401 处理**：任何工具报 401/SESSION_CREATE_FAILED → 引导用户说「登录」重新登录，**不要重试**。
- **数据不足如实说**：新品/无历史数据的 SKU 返回 0 行是正常的（采集走第三方源+1 天缓存），说明情况，不要编造指标。
- **两个采集箱别混**：`collect_products` 采集的是 Ozon 选品数据（第三方源）；`source_products` 是 1688 货源采集箱（供上架对比采购价）——语义不同，按用户意图选。
- **权限边界**：competitor_analysis / keyword_hot_words / search_products 需要更高账号权限，401 时改用 `collect_products` 的采集数据做对比，不要反复重试。
- 出卡后少写长文字总结（卡会跟随滚动被顶出视野，结论放卡里的 Badge/Alert 上）。
- **与 ozon-selection-agent 的边界**：一般选品/采集/对比用本 skill 的链路（collect_products 等，业务直连）；仅当用户明确要「竞对体检」或「Jev 打分」时才用 ozon-selection-agent 的深度链路（search_products/competitor_analysis/score_candidates，需要 agent 工具权限，401 时按本 skill 纪律引导登录或改走采集数据）。
