# SPE 账号联邦登录（外部身份源整合）

状态：proposed
类型：feature
Owner：backend/package/yuxi/services/spe_identity_service.py

## 问题

SPE WebAgent 迁移后的默认身份模型是两层分离：Yuxi 平台账号登录，SPE 凭据经对话内 `login` 工具按用户绑定（决策 2026-10-07-spe-webagent-toolkit.md 的认证节）。用户要求整合 SPE 用户系统：直接用 SPE 账号登录 Yuxi，登录即完成 SPE 身份绑定，免除对话内二次登录。

## 提案

新增 SPE 账号联邦登录：登录页提供「平台账号 / SPE 账号」双模式，`POST /api/auth/spe-token`（OAuth2 表单）→ 先调 SPE `/api/v1/auth/login` 验证 → 映射/自动开户 Yuxi 用户 → SPE JWT 写入 `user_spe_credentials`（登录即绑定）→ 签发 Yuxi JWT（响应与 `/api/auth/token` 同形）。

### 实现方案

- `services/spe_identity_service.py` 拥有映射与开户：uid 命名空间 `spe_<SPE用户名>`；首次登录自动开户（username 取 SPE 用户名、本地同名加 `(SPE)` 后缀；本地密码哈希为不可用随机值——身份源唯一是 SPE；部门自动归入 get-or-create 的「SPE 外部用户」）；已存在用户每次登录按 SPE 角色同步 Yuxi 角色（身份源拥有角色，防降权残留）。
- 角色映射（**含超管全映射，2026-10-07 用户拍板**）：SPE 角色串含 SUPER+ADMIN → superadmin、含 ADMIN → admin、其余（含未知/缺失）→ user（fail-safe 向下）。
- `auth_router` 新端点复用现有登录限速（IP+账号滑窗）与失败记录；SPE 未配置 base_url 时 400 fail-closed；SPE 凭据错误 401、上游故障 502；密码永不落盘。
- 前端三处接线：`auth_api.speLogin`、`userStore.login` 分支、登录页模式切换（radio）。

## 替代方案

- **保持两层分离（对话内 login 工具绑定）**：已实现的默认路径；体验上多一步，保留为平台账号用户的既有方式。
- **OAuth/SSO 身份源**：SPE 后端无 OAuth Provider，改造成本高且违反绞杀者原则（SPE 后端零改动）。
- **用户批量导入**：静态同步会漂移，无法处理密码校验与角色实时变化。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| SPE 账号登录自动开户并绑定 JWT | 未开户/未绑定即登录成功 | spe_identity_service | unit `test_spe_identity_service.py`（开户/绑定/同名后缀/角色同步） | 移除 upsert 绑定断言变红 | Passed |
| 角色映射含超管且未知值向下 | 错误角色放权 | `map_spe_role_to_yuxi` | unit 映射表断言 | 未知角色串 → user | Passed |
| SPE 未配置/凭据错误 fail-closed | 静默放行或泄露可探测性 | spe-token 端点 | 真机 `POST /api/auth/spe-token`（未配置返回 400 结构化错误）+ unit 401/400 | 空凭据/错密码不得产生绑定 | Passed |
| 前端 SPE 模式可达 | 模式切换不生效 | LoginView/auth_api | 手工浏览器验证 | — | Inspected |

## 风险

- **含超管全映射**（用户拍板）：SPE SUPER_ADMIN 密码即 Yuxi 平台最高权限（用户/部门/系统配置/仪表盘）。SPE 侧密码策略弱于 Yuxi 时构成提权面；已知悉并接受，如需收紧改为对等映射仅需改 `map_spe_role_to_yuxi` 一处。
- 证据口径：spe-token 未配置路径已真机验证、401/开户路径 unit 验证；前端仅做语法与存在性检查（Inspected），页面交互待用户实测。
- 真实 SPE 登录链路（staging）未验证：角色串的真实取值（SUPER_ADMIN/ADMIN/USER 为推测形状）需联调时校准映射函数；`username` 字段缺失时回落登录输入。
- 自动开户用户无手机号：API Key 等要求 department 的路径已由「SPE 外部用户」部门满足。

## 修订（2026-10-07，P0）

角色映射收紧（依据 octop-spe 工作区 spec §4.2）：SPE `SUPER_ADMIN` 由
`superadmin` 改为 `user`。理由：对外部商业租户开放平台超管是提权隐患，
平台运营改用本地 superadmin 账号。既有联邦用户在下次登录时由角色同步
逻辑自动降级，无需数据迁移。
