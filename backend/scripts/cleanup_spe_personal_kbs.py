"""清理联邦开户预建的空个人知识库（spec 2026-10-08 §6.2）。

只删 0 文件、user 档、created_by 以 spe_ 开头的库（is_empty_spe_personal_kb 判定，
系统租户库 spe_system 为 department 档天然排除）；非空库留待登录时懒改档。
默认 dry-run 仅打印，--apply 才真正删除。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
for import_path in (APP_ROOT, APP_ROOT / "package"):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))


async def main(apply: bool) -> None:
    from dotenv import load_dotenv

    # 对齐 seed_initial_users.py：.env 可能位于 backend/ 或仓库根，两处都加载
    load_dotenv(APP_ROOT / ".env", override=False)
    load_dotenv(APP_ROOT.parent / ".env", override=False)

    from yuxi.knowledge.runtime import knowledge_base
    from yuxi.services.spe_identity_service import is_empty_spe_personal_kb
    from yuxi.storage.postgres.manager import pg_manager

    # get_databases() 不做 pg_manager 懒初始化：脚本须显式初始化（对齐 seed_initial_users.py）
    pg_manager.initialize()
    try:
        targets = [kb for kb in await knowledge_base.get_databases() if is_empty_spe_personal_kb(kb)]
        for kb in targets:
            print(f"{'DELETE' if apply else 'DRY-RUN'} {kb.kb_id}  {kb.name}  created_by={kb.created_by}")
            if apply:
                await knowledge_base.delete_database(kb.kb_id)
        print(f"共 {len(targets)} 个空个人库{'已删除' if apply else '（dry-run 未执行）'}")
    finally:
        await pg_manager.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正执行删除（默认 dry-run）")
    asyncio.run(main(apply=parser.parse_args().apply))
