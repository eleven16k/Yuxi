<template>
  <div class="spe-confirm">
    <div class="sc-head">
      <span class="sc-badge" :class="data?.success ? 'ok' : 'fail'">{{ data?.success ? '执行成功' : '执行失败' }}</span>
      <span class="sc-badge dup" v-if="data?.duplicate">重复操作 · 未重复执行</span>
    </div>
    <div class="sc-summary" v-if="data?.summary">{{ data.summary }}</div>
    <div class="sc-details" v-if="detailEntries.length">
      <div class="sc-detail-row" v-for="row in detailEntries" :key="row.key">
        <span class="sc-key">{{ row.key }}</span>
        <span class="sc-value">{{ row.value }}</span>
      </div>
    </div>
    <div class="sc-op" v-if="data?.operationId">操作号：<code>{{ data.operationId }}</code></div>
    <pre v-if="!hasAnyField" class="sc-fallback">{{ JSON.stringify(props.data, null, 2) }}</pre>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  data: { type: Object, default: null }
})

const hasAnyField = computed(() => {
  const d = props.data
  return !!(d && typeof d === 'object' && ('success' in d || 'summary' in d || 'details' in d || 'operationId' in d))
})

const detailEntries = computed(() => {
  const d = props.data?.details
  if (!d || typeof d !== 'object' || Array.isArray(d)) return []
  return Object.entries(d)
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .slice(0, 12)
    .map(([key, value]) => ({ key, value: Array.isArray(value) ? `${value.length} 项` : String(value) }))
})
</script>

<style lang="less" scoped>
.spe-confirm {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid rgba(0, 0, 0, 0.06);
  background: rgba(255, 255, 255, 0.6);

  .sc-head {
    display: flex;
    gap: 6px;
  }

  .sc-badge {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 999px;

    &.ok {
      color: #1a7f37;
      background: rgba(26, 127, 55, 0.1);
    }

    &.fail {
      color: #c0392b;
      background: rgba(192, 57, 43, 0.1);
    }

    &.dup {
      color: #9a6700;
      background: rgba(234, 179, 8, 0.12);
    }
  }

  .sc-summary {
    font-size: 12px;
    color: var(--gray-900, #222);
    line-height: 1.4;
  }

  .sc-details {
    display: grid;
    grid-template-columns: minmax(80px, auto) 1fr;
    gap: 2px 12px;

    .sc-key {
      font-size: 11px;
      color: var(--gray-500, #999);
    }

    .sc-value {
      font-size: 11px;
      color: var(--gray-800, #444);
      word-break: break-word;
    }
  }

  .sc-op {
    font-size: 11px;
    color: var(--gray-500, #999);

    code {
      font-family: 'Monaco', 'Menlo', monospace;
      color: var(--gray-700, #666);
    }
  }

  .sc-fallback {
    margin: 0;
    font-size: 11px;
    white-space: pre-wrap;
    word-break: break-word;
  }
}
</style>
