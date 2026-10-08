<template>
  <div class="knowledge-center-view layout-container">
    <PageHeader title="知识中心" :show-border="true" />
    <div class="kc-content">
      <section class="kc-section">
        <div class="kc-section-head">
          <h3 class="kc-section-title">官方知识</h3>
          <span class="kc-section-desc">平台维护的共享知识，全员只读</span>
        </div>
        <ExtensionCardGrid v-if="officialDbs.length">
          <InfoCard
            v-for="db in officialDbs"
            :key="db.kb_id"
            :title="db.name"
            :subtitle="`官方 · ${db.file_count ?? 0} 份文档`"
            :description="db.description || '暂无描述'"
            @click="goDetail(db)"
          />
        </ExtensionCardGrid>
        <ResourceEmptyState v-else title="暂无官方知识" description="官方知识库尚未发布，请稍后再来。" />
      </section>

      <section class="kc-section">
        <div class="kc-section-head">
          <h3 class="kc-section-title">我的租户知识库</h3>
          <span class="kc-section-desc">{{ tenantHint }}</span>
        </div>
        <ExtensionCardGrid v-if="tenantDbs.length">
          <InfoCard
            v-for="db in tenantDbs"
            :key="db.kb_id"
            :title="db.name"
            :subtitle="`${db.file_count ?? 0} 份文档${db.can_manage ? ' · 可管理' : ' · 只读'}`"
            :description="db.description || '暂无描述'"
            @click="goDetail(db)"
          />
        </ExtensionCardGrid>
        <ResourceEmptyState
          v-else
          title="暂无租户知识库"
          description="租户知识库在首次登录时自动创建；若未出现，请退出后重新登录。"
        />
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import PageHeader from '@/components/shared/PageHeader.vue'
import ResourceEmptyState from '@/components/shared/ResourceEmptyState.vue'
import ExtensionCardGrid from '@/components/extensions/ExtensionCardGrid.vue'
import InfoCard from '@/components/shared/InfoCard.vue'
import { databaseApi } from '@/apis/knowledge_api'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()
const databases = ref([])

const officialDbs = computed(() => databases.value.filter((db) => db.is_official))
const tenantDbs = computed(() => databases.value.filter((db) => !db.is_official))
const tenantHint = computed(() =>
  userStore.isTenantAdmin
    ? '本公司共享知识库，你可以上传和管理文档'
    : '本公司共享知识库（只读，如需上传请联系公司管理员）'
)

function goDetail(db) {
  router.push({ path: `/knowledge/${db.kb_id}`, query: { from: 'knowledge' } })
}

onMounted(async () => {
  try {
    const data = await databaseApi.getAccessibleDatabases()
    databases.value = data?.databases || []
  } catch (error) {
    console.error('加载知识中心失败:', error)
    message.error('加载知识中心失败')
  }
})
</script>

<style scoped lang="less">
.knowledge-center-view {
  .kc-content {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 16px 24px;

    .kc-section {
      margin-bottom: 32px;
    }

    .kc-section-head {
      display: flex;
      align-items: baseline;
      gap: 12px;
      margin-bottom: 12px;
    }

    .kc-section-title {
      margin: 0;
      font-size: 16px;
    }

    .kc-section-desc {
      color: #999;
      font-size: 13px;
    }
  }
}
</style>
