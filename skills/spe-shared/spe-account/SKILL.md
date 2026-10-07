---
name: spe-account
tool_dependencies:
  - login
  - logout
  - spe_ping
description: SPE 运营账号身份管理——登录、退出、连通性自检。触发词：登录SPE、退出SPE、切换SPE账号、SPE连不上吗。任何 SPE 业务工具报 BIZ_AUTH_FAILED / 提示未登录时使用本 skill；业务取数/写操作请求不要用本 skill。
---
# SPE 账号身份

使用 SPE 运营工具（选品、订单、上架、知识中心等）前需要先登录 SPE：

1. **登录**：向用户索要 SPE 用户名与密码，调 `login(username, password)`。密码只在本次调用中使用、不会存储；登录成功后 JWT 自动按当前用户绑定，后续所有工具用该身份。
2. **退出/换号**：`logout`（无参数）清除绑定；换号 = logout 后再 login。
3. **连通性排查**：`spe_ping` 返回 ok 说明工具面正常；业务报错而 ping 正常时，问题在上游业务服务或账号权限。

## 纪律

- 任何工具报 401 / BIZ_AUTH_FAILED / 「未登录 SPE」→ 引导用户重新登录，**不要重试原操作**。
- SPE 账号与 Yuxi 平台账号是两层身份：不要把 Yuxi 用户名当 SPE 用户名用。
