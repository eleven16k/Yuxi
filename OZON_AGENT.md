# ozon-agent

OZON 卖家智能体平台（SPE × Yuxi）——**硬分叉产品仓**。

- **上游来源**：[xerrors/Yuxi](https://github.com/xerrors/Yuxi) @ `031e2c7`（v0.7.3）
- **分叉策略**（2026-10-07 拍板）：本仓库作为独立产品演进，**不做周期性 upstream 同步**；
  上游有需要的修复时手动 cherry-pick（`git remote -v` 中的 `upstream` 远端已保留）
- **产品定位**：SPE（OZON 中国卖家多租户 ERP）的唯一 agent 平台与知识引擎，
  卖家经 SPE 账号联邦登录使用；设计文档见工作区
  `/Users/mac/ai-plan/octop-spe/docs/superpowers/specs/2026-10-07-yuxi-ozon-agent-design.md`
- **本仓库的 SPE 侧定制**（相对上游的差异起点）：
  - `backend/package/yuxi/agents/toolkits/spe/` — SPE 工具集（C-11/C-22/C-26 契约）
  - `backend/package/yuxi/services/spe_identity_service.py` — SPE 联邦登录 + 积分入口闸
  - `backend/package/yuxi/repositories/spe_credential_repository.py` — per-user SPE 凭据
  - `backend/package/yuxi/agents/tool_approval.py` — SPE 写工具审批闸门
  - ADR：`docs/develop-guides/decisions/proposed/2026-10-07-spe-*.md`
- 本地运行手册（SPE dev 栈 × 本栈联调冒烟）：工作区
  `/Users/mac/ai-plan/octop-spe/docs/runbooks/spe-dev-local.md`
