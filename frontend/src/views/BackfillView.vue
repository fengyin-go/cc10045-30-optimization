<template>
  <section class="page" data-module="event_backfill">
    <header class="page-head">
      <div>
        <h2>事件补录稳定性工作台</h2>
        <p class="page-desc">
          外部机构补录文件逐行核对业务时间、归属与来源编号；部分接收、拒收行留痕、
          待核确认后只计一次，并按实际接收批次发布市级估算与正式地区分账。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">本班最新发布估算</span>
        <strong class="stat-value" data-testid="stat-latest-count">{{ latestCount }} 人</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">补录批次</span>
        <strong class="stat-value">{{ batches.length }} 个</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">实际接收 / 待核 / 拒收</span>
        <strong class="stat-value">{{ totals.accepted }} / {{ totals.pending }} / {{ totals.rejected }} 行</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>值班班次</span>
        <input v-model="shiftCode" data-testid="shift-input" placeholder="如 shift-night" required />
      </label>
      <label class="filter-item">
        <span>窗口起</span>
        <input v-model="windowStart" data-testid="window-start" />
      </label>
      <label class="filter-item">
        <span>窗口止</span>
        <input v-model="windowEnd" data-testid="window-end" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询本班</button>
    </form>

    <div class="bf-panel">
      <h3>1. 导入外部机构补录文件（CSV）</h3>
      <form class="bf-import" @submit.prevent="handleImport">
        <label class="filter-item">
          <span>来源机构</span>
          <input v-model="importForm.source_org" data-testid="source-org" required />
        </label>
        <label class="filter-item">
          <span>来源文件名</span>
          <input v-model="importForm.source_file" data-testid="source-file" required />
        </label>
        <label class="filter-item bf-file">
          <span>补录文件</span>
          <input type="file" accept=".csv" data-testid="backfill-file" @change="readFile" />
        </label>
        <button class="btn primary" data-testid="submit-import" type="submit" :disabled="!fileContent || importing">
          {{ importing ? '核对中…' : '导入并逐行核对' }}
        </button>
      </form>
      <p v-if="importMessage" class="bf-msg" :class="{ error: !!importError }" data-testid="import-message">
        {{ importMessage }}
      </p>
      <p class="bf-hint">
        表头：来源编号, 业务时间, 归属, 归属确认(是/否), 覆盖对象, 事件名称。
        坏数据拒收且不当零处理；归属未确认行挂「待核」，不进正式分账。
      </p>
    </div>

    <div class="bf-panel">
      <h3>2. 批次逐行核对结果</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>批次号</th>
            <th>来源机构/文件</th>
            <th>状态</th>
            <th>总行</th>
            <th>接收</th>
            <th>拒收</th>
            <th>拒收行文件</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="batch.id" :data-testid="`batch-row-${batch.id}`">
            <td>{{ batch.batch_no }}</td>
            <td>{{ batch.source_org }} / {{ batch.source_file }}</td>
            <td>{{ batch.status }}</td>
            <td>{{ batch.total_lines }}</td>
            <td>{{ batch.accepted_lines }}</td>
            <td>{{ batch.rejected_lines }}</td>
            <td>
              <a v-if="batch.rejected_file_path" :href="rejectFileUrl(batch.id)" target="_blank"
                 :data-testid="`reject-link-${batch.id}`">下载拒收行文件</a>
              <span v-else>—</span>
            </td>
            <td class="row-actions">
              <button class="link" type="button" @click="toggleLines(batch.id)">
                {{ expanded === batch.id ? '收起行明细' : '查看行明细' }}
              </button>
              <button v-if="hasPending(batch)" class="link" type="button"
                      :data-testid="`confirm-region-${batch.id}`" @click="openConfirm(batch.id)">
                补齐待核地区
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="expandedBatch" class="bf-lines">
        <h4>批次 {{ expandedBatch.batch_no }} 文件行明细</h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>文件行号</th>
              <th>来源编号</th>
              <th>业务时间</th>
              <th>归属</th>
              <th>覆盖对象</th>
              <th>行状态</th>
              <th>拒收/待核原因</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="line in expandedBatch.lines" :key="line.id"
                :data-testid="`line-${expandedBatch.id}-${line.file_line_no}`"
                :data-status="line.line_status">
              <td>{{ line.file_line_no }}</td>
              <td>{{ line.source_event_no || '—' }}</td>
              <td>{{ line.business_time || '—' }}</td>
              <td>{{ line.region || '—' }}</td>
              <td>{{ line.person_key || '—' }}</td>
              <td>{{ line.line_status }}</td>
              <td>{{ line.reject_reason || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="showConfirm" class="modal-backdrop" data-testid="confirm-modal">
      <form class="modal" @submit.prevent="handleConfirmRegion">
        <h3>补齐待核行归属地区</h3>
        <label class="create-field">
          <span>确认归属地区</span>
          <input v-model="confirmRegionName" data-testid="confirm-region-input" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showConfirm = false">取消</button>
          <button class="btn primary" data-testid="submit-confirm" type="submit">确认并计入</button>
        </div>
      </form>
    </div>

    <div class="bf-panel">
      <h3>3. 发布本班市级估算（两种口径）</h3>
      <div class="page-actions">
        <button class="btn primary" data-testid="publish-partial" type="button"
                :disabled="publishing" @click="handlePublish('partial_accepted')">
          采用已核补录（基线 + 实际接收新成员）
        </button>
        <button class="btn" data-testid="publish-hold" type="button"
                :disabled="publishing" @click="handlePublish('hold_baseline')">
          暂保旧估算（不采用待核文件）
        </button>
        <button class="btn" data-testid="publish-ledger" type="button" @click="handleLedger">
          生成正式地区分账
        </button>
      </div>
      <div v-if="latestEstimate" class="bf-estimate" data-testid="latest-estimate">
        <p>
          最新发布：<strong>{{ latestEstimate.coverage_count }} 人</strong>
          （{{ latestEstimate.policy === 'hold_baseline' ? '暂保旧估算' :
              latestEstimate.policy === 'baseline' ? '已确认旧估算基线' : '已核补录口径' }}，
          {{ latestEstimate.basis_text }}）
        </p>
        <p class="bf-hint">
          采用批次：{{ latestEstimate.scope.basis_batches.join(', ') || '无' }}；
          文件行号：{{ sortedBasisLines.join(', ') || '无' }}；窗口范围可回查。
        </p>
      </div>
    </div>

    <div class="bf-panel">
      <h3>4. 正式地区分账（仅含归属已确认事件）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>地区</th>
            <th>覆盖人数</th>
            <th>采用批次</th>
            <th>文件行号依据</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="ledger in ledgers" :key="ledger.id" :data-testid="`ledger-${ledger.region}`">
            <td>{{ ledger.region }}</td>
            <td>{{ ledger.coverage_count }}</td>
            <td>{{ ledger.basis_batch_ids }}</td>
            <td>{{ ledger.basis_line_refs }}</td>
          </tr>
          <tr v-if="!ledgers.length">
            <td colspan="4" class="empty-state">尚未生成本班正式地区分账</td>
          </tr>
        </tbody>
      </table>
    </div>

    <footer class="page-foot">
      <span v-if="actionMessage" class="bf-msg" :class="{ error: !!actionError }">{{ actionMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  confirmRegion,
  importBackfillFile,
  latestEstimate as fetchLatestEstimate,
  listBatches,
  listLedgers,
  publishEstimate,
  publishLedgers,
  rejectFileUrl,
} from '@/api/backfill'
import type { BackfillBatch, CoverageEstimate, RegionLedger } from '@/api/backfill'

const shiftCode = ref('shift-night')
const windowStart = ref('2026-11-24T00:00:00')
const windowEnd = ref('2026-11-24T23:59:59')

const batches = ref<BackfillBatch[]>([])
const ledgers = ref<RegionLedger[]>([])
const latestEstimateRow = ref<CoverageEstimate | null>(null)

const importForm = ref({ source_org: '外部救援机构A', source_file: 'backfill.csv' })
const fileContent = ref('')
const importing = ref(false)
const publishing = ref(false)
const expanded = ref<number | null>(null)
const showConfirm = ref(false)
const confirmTargetBatch = ref<number | null>(null)
const confirmRegionName = ref('')

const importMessage = ref('')
const importError = ref(false)
const actionMessage = ref('')
const actionError = ref(false)

const totals = computed(() => {
  let accepted = 0
  let rejected = 0
  let pending = 0
  for (const batch of batches.value) {
    accepted += batch.accepted_lines
    rejected += batch.rejected_lines
    pending += batch.lines.filter((line) => line.line_status === '待核').length
  }
  return { accepted, rejected, pending }
})

const latestCount = computed(() => latestEstimateRow.value?.coverage_count ?? '—')
const latestEstimate = computed(() => latestEstimateRow.value)
const expandedBatch = computed(() => batches.value.find((batch) => batch.id === expanded.value) ?? null)
const sortedBasisLines = computed(() =>
  (latestEstimateRow.value?.scope.basis_lines ?? []).map((line) => line.file_line_no).sort((a, b) => a - b),
)

function hasPending(batch: BackfillBatch): boolean {
  return batch.lines.some((line) => line.line_status === '待核')
}

function readFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  importForm.value.source_file = file.name
  const reader = new FileReader()
  reader.onload = () => {
    fileContent.value = String(reader.result ?? '')
  }
  reader.readAsText(file, 'utf-8')
}

async function handleImport() {
  importing.value = true
  importError.value = false
  importMessage.value = ''
  try {
    const result = await importBackfillFile({
      content: fileContent.value,
      source_org: importForm.value.source_org,
      source_file: importForm.value.source_file,
      shift_code: shiftCode.value,
    })
    importMessage.value =
      `批次 ${result.batch.batch_no}：${result.batch.status}，接收 ${result.batch.accepted_lines} 行，` +
      `拒收 ${result.batch.rejected_lines} 行`
    await reload()
  } catch (error) {
    importError.value = true
    importMessage.value = error instanceof Error ? error.message : '补录导入失败'
  } finally {
    importing.value = false
  }
}

function toggleLines(batchId: number) {
  expanded.value = expanded.value === batchId ? null : batchId
}

function openConfirm(batchId: number) {
  confirmTargetBatch.value = batchId
  confirmRegionName.value = ''
  showConfirm.value = true
}

async function handleConfirmRegion() {
  if (confirmTargetBatch.value === null) return
  try {
    await confirmRegion(confirmTargetBatch.value, confirmRegionName.value)
    showConfirm.value = false
    actionError.value = false
    actionMessage.value = '待核行已确认并计入实际接收批次'
    await reload()
  } catch (error) {
    actionError.value = true
    actionMessage.value = error instanceof Error ? error.message : '地区确认失败'
  }
}

async function handlePublish(policy: 'partial_accepted' | 'hold_baseline') {
  publishing.value = true
  try {
    const result = await publishEstimate({
      shift_code: shiftCode.value,
      window_start: windowStart.value,
      window_end: windowEnd.value,
      policy,
    })
    latestEstimateRow.value = result.estimate
    actionError.value = false
    actionMessage.value = `已发布市级估算：${result.estimate.coverage_count} 人`
  } catch (error) {
    actionError.value = true
    actionMessage.value = error instanceof Error ? error.message : '估算发布失败'
  } finally {
    publishing.value = false
  }
}

async function handleLedger() {
  try {
    const result = await publishLedgers({
      shift_code: shiftCode.value,
      window_start: windowStart.value,
      window_end: windowEnd.value,
      estimate_id: latestEstimateRow.value?.id,
    })
    ledgers.value = result.ledgers
    actionError.value = false
    actionMessage.value = `已生成 ${result.ledgers.length} 个地区的正式分账`
  } catch (error) {
    actionError.value = true
    actionMessage.value = error instanceof Error ? error.message : '分账生成失败'
  }
}

async function reload() {
  const [batchResult, estimateResult, ledgerResult] = await Promise.all([
    listBatches(shiftCode.value),
    fetchLatestEstimate(shiftCode.value),
    listLedgers(shiftCode.value),
  ])
  batches.value = batchResult.items
  latestEstimateRow.value = estimateResult.estimate
  ledgers.value = ledgerResult.items
  if (expanded.value !== null && !batches.value.some((batch) => batch.id === expanded.value)) {
    expanded.value = null
  }
}

onMounted(reload)
</script>
