<template>
  <div class="spe-product-table">
    <div class="spe-pt-row" v-for="(row, i) in displayRows" :key="i">
      <img v-if="imageOf(row)" :src="imageOf(row)" class="spe-pt-thumb" loading="lazy" referrerpolicy="no-referrer" @error="onImgError($event)" />
      <div class="spe-pt-main">
        <div class="spe-pt-title">
          <span class="spe-pt-rank" v-if="rankBase">{{ rankBase + i }}</span>
          <span class="spe-pt-name">{{ titleOf(row) }}</span>
        </div>
        <div class="spe-pt-chips">
          <span class="spe-pt-chip" v-for="chip in chipsOf(row)" :key="chip.label">
            <span class="chip-label">{{ chip.label }}</span>
            <span class="chip-value">{{ chip.value }}</span>
          </span>
        </div>
      </div>
      <a v-if="linkOf(row)" :href="linkOf(row)" target="_blank" rel="noopener" class="spe-pt-link" @click.stop>打开</a>
    </div>
    <div class="spe-pt-more" v-if="rows.length > displayRows.length">
      共 {{ rows.length }} 条，已显示前 {{ displayRows.length }} 条
    </div>
    <div class="spe-pt-empty" v-if="!rows.length">暂无数据</div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  data: { type: [Object, Array], default: null }
})

const rows = computed(() => {
  const d = props.data
  if (Array.isArray(d)) return d
  if (d && typeof d === 'object') {
    for (const key of ['content', 'rows', 'records', 'items', 'list']) {
      if (Array.isArray(d[key])) return d[key]
    }
  }
  return []
})

const displayRows = computed(() => rows.value.slice(0, 20))
const rankBase = computed(() => {
  const d = props.data
  const offset = d && typeof d === 'object' ? Number(d.number ?? d.offset ?? NaN) : NaN
  return Number.isFinite(offset) ? offset + 1 : 0
})

const titleKeys = ['title', 'name', 'productName', 'product_name', 'sku_title', 'sku']
const imageKeys = ['mainImage', 'imageUrl', 'image', 'img', 'pic', 'picture', 'photo']
const linkKeys = ['productUrl', 'product_url', 'url', 'link', 'detailUrl']

const pick = (row, keys) => {
  for (const k of keys) {
    const v = row?.[k]
    if (v && typeof v === 'string' && v.trim()) return v.trim()
  }
  return ''
}

const titleOf = (row) => pick(row, titleKeys) || '（无标题）'
const imageOf = (row) => pick(row, imageKeys)
const linkOf = (row) => pick(row, linkKeys)

// 常用指标字段（存在才显示）
const METRICS = [
  ['price', '价格'],
  ['priceMin', '起价'],
  ['avgPrice', '均价'],
  ['sales', '销量'],
  ['monthlySales', '月销'],
  ['revenue', '销售额'],
  ['salesRate', '增速'],
  ['grossMargin', '毛利率'],
  ['reviewCount', '评论'],
  ['sellers', '卖家数'],
  ['stock', '库存'],
  ['status', '状态']
]

const chipsOf = (row) => {
  const chips = []
  for (const [key, label] of METRICS) {
    const v = row?.[key]
    if (v === null || v === undefined || v === '') continue
    const text = typeof v === 'number' ? formatNumber(v) : String(v)
    if (text) chips.push({ label, value: text })
    if (chips.length >= 6) break
  }
  return chips
}

const formatNumber = (n) => {
  if (!Number.isFinite(n)) return String(n)
  if (Math.abs(n) >= 10000) return (n / 10000).toFixed(1) + '万'
  if (Number.isInteger(n)) return String(n)
  return String(Math.round(n * 100) / 100)
}

const onImgError = (e) => {
  e.target.style.display = 'none'
}
</script>

<style lang="less" scoped>
.spe-product-table {
  display: flex;
  flex-direction: column;
  gap: 6px;

  .spe-pt-row {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    padding: 8px;
    border: 1px solid rgba(0, 0, 0, 0.06);
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.6);
  }

  .spe-pt-thumb {
    width: 44px;
    height: 44px;
    border-radius: 6px;
    object-fit: cover;
    flex-shrink: 0;
    background: rgba(0, 0, 0, 0.04);
  }

  .spe-pt-main {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .spe-pt-title {
    display: flex;
    gap: 6px;
    align-items: baseline;

    .spe-pt-rank {
      flex-shrink: 0;
      font-size: 11px;
      color: var(--gray-500, #999);
    }

    .spe-pt-name {
      font-size: 12px;
      line-height: 1.4;
      color: var(--gray-900, #222);
      word-break: break-word;
    }
  }

  .spe-pt-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
  }

  .spe-pt-chip {
    font-size: 11px;

    .chip-label {
      color: var(--gray-500, #999);
      margin-right: 3px;
    }

    .chip-value {
      color: var(--gray-800, #444);
      font-weight: 500;
    }
  }

  .spe-pt-link {
    flex-shrink: 0;
    font-size: 11px;
    color: var(--brand-500, #4f6ef7);
    text-decoration: none;
  }

  .spe-pt-more,
  .spe-pt-empty {
    font-size: 11px;
    color: var(--gray-500, #999);
    text-align: center;
    padding: 4px 0;
  }
}
</style>
