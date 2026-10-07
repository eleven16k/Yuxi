<template>
  <div class="spe-data-table">
    <div class="spe-dt-kv" v-if="panel && kvRows.length">
      <div class="kv-row" v-for="row in kvRows" :key="row.key">
        <span class="kv-key">{{ row.key }}</span>
        <span class="kv-value">{{ row.value }}</span>
      </div>
    </div>

    <div class="spe-dt-scroll" v-else-if="columns.length && rows.length">
      <table class="spe-dt">
        <thead>
          <tr>
            <th v-for="col in columns" :key="col">{{ col }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in displayRows" :key="i">
            <td v-for="col in columns" :key="col">{{ cellText(row[col]) }}</td>
          </tr>
        </tbody>
      </table>
      <div class="spe-dt-more" v-if="rows.length > displayRows.length">
        共 {{ rows.length }} 行，已显示前 {{ displayRows.length }} 行
      </div>
    </div>

    <pre v-else class="spe-fallback">{{ JSON.stringify(props.data, null, 2) }}</pre>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  data: { type: [Object, Array], default: null },
  panel: { type: Boolean, default: false }
})

const rows = computed(() => {
  const d = props.data
  if (Array.isArray(d)) return d.filter((r) => r && typeof r === 'object')
  if (d && typeof d === 'object') {
    for (const key of ['content', 'rows', 'records', 'items', 'list']) {
      if (Array.isArray(d[key])) return d[key].filter((r) => r && typeof r === 'object')
    }
  }
  return []
})

// 面板模式：对象本身按 key-value 呈现（含常见的嵌套 summary/stats 提升一层）
const kvRows = computed(() => {
  const d = props.data
  if (!d || typeof d !== 'object' || Array.isArray(d)) return []
  const entries = []
  const collect = (obj, prefix = '') => {
    for (const [k, v] of Object.entries(obj)) {
      if (v === null || v === undefined) continue
      if (v && typeof v === 'object' && !Array.isArray(v) && ['summary', 'stats', 'total', 'totals', 'overview'].includes(k)) {
        collect(v, `${prefix}${k}.`)
        continue
      }
      entries.push({ key: prefix + k, value: Array.isArray(v) ? `${v.length} 项` : String(v) })
    }
  }
  collect(d)
  return entries.slice(0, 24)
})

const columns = computed(() => {
  const keys = []
  for (const row of rows.value.slice(0, 10)) {
    for (const k of Object.keys(row)) {
      if (!keys.includes(k) && keys.length < 8) keys.push(k)
    }
  }
  return keys
})

const displayRows = computed(() => rows.value.slice(0, 30))

const cellText = (v) => {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'object') return Array.isArray(v) ? `${v.length} 项` : '{…}'
  const s = String(v)
  return s.length > 40 ? s.slice(0, 40) + '…' : s
}
</script>

<style lang="less" scoped>
.spe-data-table {
  .spe-dt-kv {
    display: grid;
    grid-template-columns: minmax(90px, auto) 1fr;
    gap: 3px 12px;
    padding: 8px;
    border-radius: 6px;
    background: rgba(0, 0, 0, 0.02);

    .kv-key {
      font-size: 11px;
      color: var(--gray-500, #999);
    }

    .kv-value {
      font-size: 12px;
      color: var(--gray-900, #222);
      word-break: break-word;
    }
  }

  .spe-dt-scroll {
    overflow-x: auto;
    border-radius: 6px;
    border: 1px solid rgba(0, 0, 0, 0.06);
  }

  .spe-dt {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;

    th,
    td {
      padding: 5px 8px;
      text-align: left;
      white-space: nowrap;
      border-bottom: 1px solid rgba(0, 0, 0, 0.05);
    }

    th {
      color: var(--gray-500, #999);
      font-weight: 500;
      background: rgba(0, 0, 0, 0.02);
      position: sticky;
      top: 0;
    }

    td {
      color: var(--gray-800, #444);
    }

    tr:last-child td {
      border-bottom: none;
    }
  }

  .spe-dt-more {
    font-size: 11px;
    color: var(--gray-500, #999);
    text-align: center;
    padding: 4px 0;
  }

  .spe-fallback {
    margin: 0;
    font-size: 11px;
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
