<template>
  <BaseToolCall :tool-call="toolCall">
    <template #header>
      <div class="sep-header">
        <span class="note">SPE 数据</span>
        <span class="separator" v-if="envelopeOf(resultContent) && envelopeOf(resultContent).ui">|</span>
        <span class="description" v-if="envelopeOf(resultContent) && envelopeOf(resultContent).ui">
          {{ cardLabelOf(envelopeOf(resultContent).ui) }}
        </span>
      </div>
    </template>

    <template #result="{ resultContent }">
      <div class="spe-block">
        <SpeProductTable v-if="envelopeOf(resultContent)?.ui === 'product-table'" :data="envelopeOf(resultContent).data" />
        <SpeDataTable v-else-if="['data-table', 'panel'].includes(envelopeOf(resultContent)?.ui)" :data="envelopeOf(resultContent).data" :panel="envelopeOf(resultContent).ui === 'panel'" />
        <SpeConfirmResult v-else-if="envelopeOf(resultContent)?.ui === 'confirm-result'" :data="envelopeOf(resultContent).data" />
        <pre v-else-if="parsedOf(resultContent) !== null" class="spe-fallback">{{ JSON.stringify(parsedOf(resultContent), null, 2) }}</pre>
      </div>
    </template>
  </BaseToolCall>
</template>

<script setup>
import BaseToolCall from '../BaseToolCall.vue'
import SpeProductTable from './SpeProductTable.vue'
import SpeDataTable from './SpeDataTable.vue'
import SpeConfirmResult from './SpeConfirmResult.vue'

const props = defineProps({
  toolCall: {
    type: Object,
    required: true
  }
})

const CARD_LABELS = {
  'product-table': '商品列表',
  'data-table': '数据表',
  'panel': '数据面板',
  'confirm-result': '操作回执',
  'score-report': '评分报告',
  'trend-chart': '趋势图'
}

// SPE 工具结果信封：{blockId, ui, v, data}；无信封时回落原始 JSON
const parsedOf = (content) => {
  if (content === null || content === undefined) return null
  if (typeof content === 'object') return content
  if (typeof content === 'string') {
    try {
      return JSON.parse(content)
    } catch {
      return null
    }
  }
  return null
}

const envelopeOf = (content) => {
  const value = parsedOf(content)
  if (value && typeof value === 'object' && typeof value.ui === 'string' && 'data' in value) {
    return value
  }
  return null
}

const cardLabelOf = (ui) => CARD_LABELS[ui] || ui || ''
</script>

<style lang="less" scoped>
.spe-block {
  .spe-fallback {
    margin: 0;
    font-size: 11px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
    padding: 8px;
    border-radius: 6px;
    background: rgba(0, 0, 0, 0.03);
    max-height: 240px;
    overflow: auto;
    font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  }
}
</style>
