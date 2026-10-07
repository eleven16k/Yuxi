"""知识文件上传用例（从 knowledge_router 抽出的 service Owner）。

upload 步骤的业务逻辑（扩展名校验、内容哈希、同内容去重、MinIO 上传）
原长在路由函数内；SPE WebAgent 的 kb_upload 内化（批次 3B）需要进程内
入口，按「路由薄、service 拥有用例」抽出，路由与工具共用同一份逻辑。
"""

from __future__ import annotations

import os
import time
from typing import Any

from yuxi.knowledge.parser.capabilities import is_supported_file_extension
from yuxi.knowledge.utils import calculate_content_hash
from yuxi.storage.minio.client import MinIOClient, aupload_file_to_minio
from yuxi.utils.upload_utils import MAX_UPLOAD_SIZE_BYTES


class KnowledgeFileDuplicateError(Exception):
    """同内容文件已存在（HTTP 409 语义）。"""


class KnowledgeFileTypeError(Exception):
    """不支持的文件类型（HTTP 400 语义）。"""


def _kb():
    from yuxi.knowledge.runtime import knowledge_base

    return knowledge_base


async def upload_knowledge_file_bytes(
    *,
    kb_id: str | None,
    filename: str,
    file_bytes: bytes,
    max_size_bytes: int = MAX_UPLOAD_SIZE_BYTES,
) -> dict[str, Any]:
    """校验并上传文件字节到知识库文档桶，返回上传记录字段。

    同内容去重先于上传（409 语义）；文件名统一小写，MinIO 对象名带时间戳。
    """
    if not filename:
        raise ValueError("No selected file")
    if not is_supported_file_extension(filename):
        ext = os.path.splitext(filename)[1].lower()
        raise KnowledgeFileTypeError(f"Unsupported file type: {ext}")
    if len(file_bytes) > max_size_bytes:
        raise ValueError(f"文件过大，当前仅支持 {max_size_bytes // 1024 // 1024} MB 以内的文件")

    basename, ext = os.path.splitext(filename)
    normalized = f"{basename}{ext}".lower()

    content_hash = await calculate_content_hash(file_bytes)
    if await _kb().file_existed_in_db(kb_id, content_hash):
        raise KnowledgeFileDuplicateError(
            "数据库中已经存在了相同内容文件，File with the same content already exists in this database"
        )

    timestamp = int(time.time() * 1000)
    minio_filename = f"{basename}_{timestamp}{ext}"
    bucket_name = MinIOClient.KB_BUCKETS["documents"]
    folder = kb_id if kb_id else "unknown"
    object_name = f"{folder}/upload/{minio_filename}"
    minio_url = await aupload_file_to_minio(bucket_name, object_name, file_bytes)

    same_name_files = await _kb().get_same_name_files(kb_id, normalized)
    return {
        "file_path": minio_url,
        "minio_path": minio_url,
        "kb_id": kb_id,
        "content_hash": content_hash,
        "filename": normalized,
        "original_filename": basename,
        "size": len(file_bytes),
        "minio_filename": minio_filename,
        "object_name": object_name,
        "bucket_name": bucket_name,
        "same_name_files": same_name_files,
        "has_same_name": len(same_name_files) > 0,
    }
