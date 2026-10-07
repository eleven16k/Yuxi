---
name: source-listing-workflow
tool_dependencies:
  - source_products
  - list_stores
  - generate_listing
  - get_source_draft
  - host_source_images
  - generate_ozon_images
  - build_rich_content
  - generate_variant_titles
description: 1688 货源上架工作流编排——采集箱选货 → 生成草稿 → 托管图 → AI 套图 → 富内容 → 变体标题。触发词：上架、生成草稿、富内容、AI 套图、变体标题、1688 货源上架、发布到 Ozon。当用户想把 1688 货源上架到 Ozon 或提到草稿/富内容/套图时使用本 skill；选品对比阶段请用 product-selection-workflow。
---
# 1688 货源上架工作流

目标：把采集箱（1688 货源）里的商品一路加工到「可上架」状态。全程按序推进，每步都是写操作（弹审批卡、按次扣积分），**不要跳步、不要并行**。

## 前置

- 用户给了 1688 货源（已在采集箱）：用 `source_products` 确认源货在箱内并拿到 sourceProductId。
- 用户给了 Ozon SKU → 先用 `product-selection-workflow` 的链路（`collect_products`）确认品，再回本工作流。
- 确认店铺：`list_stores`（generate_listing 需要 store，不传默认首个启用店铺）。

## 流程（按序）

### ① 生成上架草稿
`generate_listing`（sourceProductId 必填，store 可选）——同步 1–3 分钟，弹审批，按次扣积分。完成回执含俄语标题/定价/类目/HS 码。

### ② 查看草稿核对
`get_source_draft`（sourceProductId）——字段/值面板卡：标题、定价、类目、HS 码、卖点、富内容有无、生图状态、变体数。给用户过目，问是否要调整（调整走 SPE 审核页，对话里不 editing）。

### ③ 托管源货图片
`host_source_images`（sourceProductId）——1688 图下载到 SPE 公网图床，Ozon 拉图与富内容都依赖这一步。

### ④ AI 生成 Ozon 套图（可选，用户要才做）
`generate_ozon_images`（sourceProductId，count 可选）——**异步启动**（49 积分/张，弹审批）。启动后用 `get_source_draft` 轮询「生图状态」（running → 完成），把已生成图数报给用户。积分离谱时先提醒再启动。

### ⑤ 生成富内容
`build_rich_content`（sourceProductId）——组装 Ozon 商品卡图文 JSON 写回草稿（按次扣 10 积分，弹审批）。AI 图优先、托管原图兜底，所以放在④之后。

### ⑥ 变体标题（拆卡铺货才做）
`generate_variant_titles`（sourceProductId）——为每个 SKU 规格生成俄语变体标题（按次扣积分，AI 失败自动回退拼接）。单规格商品跳过此步。

### ⑦ 上架
**publish 不在工作台**——引导用户到 SPE 审核页人工确认上架（对话里说明「最后一步在页面完成」）；页面跳转链接随后续批次开放。

## 纪律

- 401/SESSION_CREATE_FAILED → 引导「登录」，不要重试。
- 积分不足（BIZ_INSUFFICIENT_CREDIT）→ 告知充值，不要重试。
- 每步之间把回执摘要报给用户（做了什么、扣了多少），**不要静默连跑**。
- 出卡后少写长文字（卡片会被滚动顶出视野，结论放卡里）。
