from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
class TestMinIOClientStatFile:
    """Test MinIOClient.stat_file and astat_file methods."""

    def test_stat_file_returns_size(self):
        from yuxi.storage.minio.client import MinIOClient

        client = MinIOClient()
        mock_stat = MagicMock()
        mock_stat.size = 1024
        client._client = MagicMock()
        client._client.stat_object.return_value = mock_stat

        result = client.stat_file("knowledgebases", "db/upload/test.pdf")
        assert result == 1024
        client._client.stat_object.assert_called_once_with(
            bucket_name="knowledgebases", object_name="db/upload/test.pdf"
        )

    def test_stat_file_returns_none_when_not_found(self):
        from io import BytesIO

        from minio.error import S3Error
        from urllib3 import HTTPResponse

        from yuxi.storage.minio.client import MinIOClient

        client = MinIOClient()
        client._client = MagicMock()
        resp = HTTPResponse(BytesIO(b""), status=404)
        client._client.stat_object.side_effect = S3Error(
            resp, "NoSuchKey", "Not found", "resource", "request_id", "host_id"
        )

        result = client.stat_file("knowledgebases", "db/upload/missing.pdf")
        assert result is None

    @pytest.mark.asyncio
    async def test_astat_file_returns_size(self):
        from yuxi.storage.minio.client import MinIOClient

        client = MinIOClient()
        mock_stat = MagicMock()
        mock_stat.size = 2048
        client._client = MagicMock()
        client._client.stat_object.return_value = mock_stat

        result = await client.astat_file("knowledgebases", "db/upload/test.pdf")
        assert result == 2048


@pytest.mark.unit
class TestAddFileRecordSizeFallback:
    """Test that add_file_record fills size from MinIO when not provided."""

    @pytest.fixture(autouse=True)
    def _stub_tenant_kb_quota(self):
        """扼制点配额为函数内惰性 import：桩必须打在源模块属性上（无 PG 环境不触发真实判定）。"""

        async def _noop(kb_id, **kwargs):
            return None

        with patch("yuxi.services.knowledge_quota_service.ensure_tenant_kb_quota", _noop):
            yield

    def _make_test_kb(self, work_dir="/tmp/test_kb"):
        from yuxi.knowledge.base import KnowledgeBase

        class TestKB(KnowledgeBase):
            @property
            def kb_type(self):
                return "test"

            async def _create_kb_instance(self, kb_id, embedding_model_spec: str | None):
                pass

            async def _initialize_kb_instance(self, instance):
                pass

            async def _persist_file_meta(self, file_id, meta):
                pass

            async def index_file(self, kb_id, file_id, operator_id=None):
                return {}

            async def aquery(self, query_text, kb_id, **kwargs):
                return []

            async def delete_file(self, kb_id, file_id):
                pass

            async def get_file_basic_info(self, kb_id, file_id):
                return {}

            async def get_file_content(self, kb_id, file_id):
                return {}

            async def get_file_info(self, kb_id, file_id):
                return {}

            async def get_query_params_config(self, kb_id, **kwargs):
                return {"type": "test", "options": []}

        kb = TestKB(work_dir=work_dir)
        return kb

    @pytest.mark.asyncio
    async def test_add_file_record_fetches_size_from_minio_when_missing(self):
        kb = self._make_test_kb()

        item = "minio://knowledgebases/db1/upload/test_1234567890123.pdf"
        params = {
            "content_type": "file",
            "content_hashes": {item: "abc123"},
        }

        mock_minio = AsyncMock()
        mock_minio.astat_file.return_value = 9999

        with patch("yuxi.storage.minio.get_minio_client", return_value=mock_minio):
            metadata = await kb.add_file_record(
                "db1",
                item,
                params=params,
                additional_params={},
            )

            assert metadata["size"] == 9999
            mock_minio.astat_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_file_record_keeps_provided_size(self):
        kb = self._make_test_kb()

        item = "minio://knowledgebases/db1/upload/test_1234567890123.pdf"
        params = {
            "content_type": "file",
            "content_hashes": {item: "abc123"},
            "file_sizes": {item: 5555},
        }

        mock_minio = AsyncMock()

        with patch("yuxi.storage.minio.get_minio_client", return_value=mock_minio):
            metadata = await kb.add_file_record(
                "db1",
                item,
                params=params,
                additional_params={},
            )

            assert metadata["size"] == 5555
            mock_minio.astat_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_file_record_size_fallback_handles_error_gracefully(self):
        kb = self._make_test_kb()

        item = "minio://knowledgebases/db1/upload/test_1234567890123.pdf"
        params = {
            "content_type": "file",
            "content_hashes": {item: "abc123"},
        }

        mock_minio = AsyncMock()
        mock_minio.astat_file.side_effect = Exception("MinIO connection error")

        with patch("yuxi.storage.minio.get_minio_client", return_value=mock_minio):
            metadata = await kb.add_file_record(
                "db1",
                item,
                params=params,
                additional_params={},
            )

            assert metadata.get("size") is None


@pytest.mark.unit
class TestAddFileRecordQuotaChokepoint:
    """扼制点（spec 2026-10-08 §8）：登记路径权威配额判定在 size 已知后、写入前。

    走真实 ensure_tenant_kb_quota——只桩其惰性 import 的源模块属性
    （knowledge_base 运行时单例 / spe_kb_quota_opts），验证 add_file_record
    内的接线与字节累计语义。
    """

    ITEM = "minio://knowledgebases/db1/upload/big_1234567890123.pdf"

    def _make_test_kb(self, persisted):
        from yuxi.knowledge.base import KnowledgeBase

        class TestKB(KnowledgeBase):
            kb_type = "test"

            async def _create_kb_instance(self, kb_id, embedding_model_spec: str | None):
                pass

            async def _initialize_kb_instance(self, instance):
                pass

            async def _persist_file_meta(self, file_id, meta):
                persisted.append({"file_id": file_id, "size": meta.get("size")})

            async def index_file(self, kb_id, file_id, operator_id=None, **kwargs):
                return {}

            async def aquery(self, query_text, kb_id, **kwargs):
                return []

            def get_query_params_config(self, kb_id, **kwargs):
                return {"type": "test", "options": []}

            async def delete_file(self, kb_id, file_id):
                pass

            async def get_file_basic_info(self, kb_id, file_id):
                return {}

            async def get_file_content(self, kb_id, file_id):
                return {}

            async def get_file_info(self, kb_id, file_id):
                return {}

        return TestKB(work_dir="/tmp/test_kb")

    @pytest.fixture
    def quota_env(self, monkeypatch):
        from yuxi.config import options as options_mod
        from yuxi.knowledge import runtime as kb_runtime

        persisted: list[dict] = []

        async def fake_info(kb_id):
            # total_size 随已登记字节累计——模拟真实库的 stats 状态
            return SimpleNamespace(
                kb_id=kb_id,
                file_count=len(persisted),
                total_size=sum(int(p["size"] or 0) for p in persisted),
                share_config={
                    "version": 2,
                    "read_scope": {"access_level": "department", "department_ids": [7], "user_uids": []},
                    "manage_scope": None,
                },
            )

        class _Opts:
            async def get(self):
                return {"max_file_count": "100", "max_total_bytes": "10000"}

        monkeypatch.setattr(kb_runtime, "knowledge_base", SimpleNamespace(get_database_info=fake_info))
        monkeypatch.setattr(options_mod, "spe_kb_quota_opts", _Opts())
        return persisted

    def _params(self, size: int) -> dict:
        return {
            "content_type": "file",
            "content_hashes": {self.ITEM: "abc123"},
            "file_sizes": {self.ITEM: size},
        }

    @pytest.mark.asyncio
    async def test_duplicate_registration_rejected_by_byte_accumulation(self, quota_env):
        """重复登记同一大文件：首登放行，第二次因字节累计超限被拒且不再落库。"""
        from yuxi.services.knowledge_quota_service import TenantKBQuotaExceededError

        kb = self._make_test_kb(quota_env)

        first = await kb.add_file_record("db1", self.ITEM, params=self._params(6000), additional_params={})
        assert first["size"] == 6000
        assert len(quota_env) == 1  # 首次登记已写入

        with pytest.raises(TenantKBQuotaExceededError) as info:
            await kb.add_file_record("db1", self.ITEM, params=self._params(6000), additional_params={})
        assert "字节" in str(info.value)  # 字节维度（6000 + 6000 > 10000），非文件数
        assert len(quota_env) == 1  # 拒绝发生在 _persist_file_meta 之前

    @pytest.mark.asyncio
    async def test_official_kb_exempt_at_chokepoint(self, quota_env, monkeypatch):
        """官方库（read_scope access_level=global）在扼制点同样豁免。"""
        from yuxi.knowledge import runtime as kb_runtime

        async def fake_info(kb_id):
            return SimpleNamespace(
                kb_id=kb_id,
                file_count=10**9,
                total_size=10**12,
                share_config={
                    "version": 2,
                    "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
                    "manage_scope": None,
                },
            )

        monkeypatch.setattr(kb_runtime, "knowledge_base", SimpleNamespace(get_database_info=fake_info))
        kb = self._make_test_kb(quota_env)
        await kb.add_file_record("db1", self.ITEM, params=self._params(6000), additional_params={})  # 不抛
