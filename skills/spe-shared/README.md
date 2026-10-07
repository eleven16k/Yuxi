# skills/spe-shared — SPE 共享 Skill 源（repo 化备份）

原仅存于本机 `docker/volumes/yuxi/skill-sources/shared/`（git 忽略、随容器卷易失），
2026-10-08 P1#01 入库固化。**本目录是源；运行态以 Yuxi 数据库 + skill-sources 卷为准。**

## 内容

| Skill | 用途 | 工具依赖 |
|---|---|---|
| spe-account | 账号/登录态自检 | login/logout/spe_ping |
| knowledge-center | 知识中心导航 | kb_list/kb_read/yuxi_kb_search |
| product-selection-workflow | 选品全流程 | 6 只读工具链 |
| source-listing-workflow | 1688 采集→上架 | 8 工具链（含采集/生成/发布） |
| ozon-selection-agent | 选品智能体人设 | 含 references/report-structure.md |

## 换环境重装

1. 打包上传：将各 Skill 目录打成 zip，经 Dashboard「技能 → 上传安装」
   （或 `POST /api/skills/upload`，draft→prepare→confirm 链，见
   `backend/server/routers/skill_router.py`）。
2. 安装为**共享 Skill**（skill-sources/shared/<name>/），各智能体按
   `preload_skills` 引用。
3. 依赖对账：ADR「26 项工具依赖对账全命中」见
   `docs/develop-guides/decisions/proposed/2026-10-07-spe-webagent-toolkit.md`。

## 同步纪律

Skill 内容修订：先改本目录（源），再经安装 API 更新运行态；勿直接改卷内文件。
