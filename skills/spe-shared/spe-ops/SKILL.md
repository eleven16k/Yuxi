---
name: spe-ops
tool_dependencies:
  - list_orders
  - confirm_order
  - ship_order
  - cancel_order
  - refund_order
  - list_products
  - inventory_health
  - low_stock
  - replenishment
  - ops_plans
  - store_metrics
  - dashboard_stats
  - monthly_report
  - credit_transactions
  - keyword_hot_words
  - preview_dynamic_price
  - apply_dynamic_price
  - restock_inventory
  - kb_upload
  - yuxi_research
description: SPE 运营操作台——订单查询与状态操作（确认/发货/取消/退款，写需审批）、库存与补货、动态定价、经营指标（店铺/仪表盘/月报/积分/热词）、知识库上传与深度研究。触发词：查订单、库存、补货、改价/调价、动态定价、经营数据、店铺指标、月报、积分明细、搜索热词、存入知识库、深度研究。选品与 1688 上架请用对应专项 skill。
---
# SPE 运营操作台

订单、库存、定价、经营指标与知识沉淀的统一操作面。

## 读操作

- **订单**：`list_orders`（状态/日期过滤）
- **库存**：`inventory_health`、`low_stock`、`replenishment`（补货建议）、`ops_plans`
- **商品**：`list_products`
- **指标**：`store_metrics`、`dashboard_stats`、`monthly_report`、`credit_transactions`、`keyword_hot_words`

## 写操作（必须人工审批）

所有写工具已挂审批闸：模型发起 → 平台弹审批卡 → 卖家批准后才执行。

1. **动态定价**：先 `preview_dynamic_price` 出推荐与影响面，向用户确认后再 `apply_dynamic_price`
2. **补货**：`restock_inventory` 前复述 SKU、数量、目标仓库
3. **知识沉淀**：`kb_upload`（.md/.txt ≤1MB）存经营资料；`yuxi_research` 发起深度研究（异步，自动轮询）

## 纪律

- 写操作前先讲清影响面（哪些 SKU、改什么、可回滚吗），批准后执行
- 回执含 `duplicate` 标记 = 幂等重放，未重复执行——如实告知用户
- 失败时按错误码向用户解释（AUTH→重新登录；UPSTREAM→稍后重试；VALIDATION→参数问题）
