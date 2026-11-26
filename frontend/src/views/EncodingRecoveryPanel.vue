<template>
  <section class="recovery-panel" data-testid="encoding-recovery-panel">
    <header class="recovery-head">
      <div>
        <h3>编码恢复</h3>
        <p class="page-desc">
          无法识别的编码声明单独登记为失败编码记录，原字节与当时的声明原样保留；
          真正空附件按合法空输入核定，两者不会混为一谈。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="toggle-attach-form" @click="showAttach = !showAttach">
          {{ showAttach ? '收起附件登记' : '登记附件（含编码声明）' }}
        </button>
        <button class="btn" type="button" data-testid="refresh-recoveries" @click="loadList">
          刷新失败清单
        </button>
      </div>
    </header>

    <form
      v-if="showAttach"
      class="analysis-form"
      data-testid="attach-form"
      @submit.prevent="submitAttachment"
    >
      <label>
        <span>关联批次</span>
        <input v-model="attach.batch" data-testid="attach-batch" required />
      </label>
      <label>
        <span>附件名</span>
        <input v-model="attach.attachment" data-testid="attach-name" />
      </label>
      <label>
        <span>原始字节（HEX；留空表示 0 字节空附件）</span>
        <input
          v-model="attach.bytes_hex"
          data-testid="attach-bytes"
          placeholder="例如 e7bc96…（留空 = 空附件）"
        />
      </label>
      <label>
        <span>编码声明</span>
        <input v-model="attach.declared_encoding" data-testid="attach-declared" placeholder="unknown-charset" />
      </label>
      <label>
        <span>目标编码</span>
        <input v-model="attach.target_encoding" data-testid="attach-target" />
      </label>
      <button class="btn primary" type="submit" data-testid="submit-attach" :disabled="busy">
        登记并转码
      </button>
    </form>
    <p v-if="attachMessage" class="ok-text" data-testid="attach-message">{{ attachMessage }}</p>
    <p v-if="attachError" class="error-text" data-testid="attach-error">{{ attachError }}</p>

    <p v-if="listError" class="error-text" data-testid="recovery-list-error">{{ listError }}</p>

    <table class="data-table" data-testid="recovery-table">
      <thead>
        <tr>
          <th>恢复编号</th>
          <th>关联批次</th>
          <th>附件</th>
          <th>当时的编码声明</th>
          <th>类型</th>
          <th>原字节</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in failureRows"
          :key="item.恢复编号"
          :data-testid="`recovery-row-${item.恢复编号}`"
        >
          <td>{{ item.恢复编号 }}</td>
          <td>{{ item.关联批次 }}</td>
          <td>{{ item.附件名 || '—' }}</td>
          <td>{{ item.编码声明 }}</td>
          <td>{{ item.记录类型 }}</td>
          <td>{{ item.原始字节数 }} 字节</td>
          <td>{{ item.status }}</td>
          <td>
            <button
              class="link"
              type="button"
              :data-testid="`open-recovery-${item.恢复编号}`"
              @click="openDetail(item.恢复编号)"
            >
              打开失败详情
            </button>
          </td>
        </tr>
        <tr v-if="!failureRows.length">
          <td colspan="8" class="empty-state">暂无失败编码记录</td>
        </tr>
      </tbody>
    </table>

    <div v-if="detail" class="recovery-detail" data-testid="recovery-detail">
      <header class="recovery-detail-head">
        <h4>失败详情 · {{ detail.恢复编号 }}</h4>
        <div class="page-actions">
          <span class="badge" :data-testid="`detail-status-${detail.status}`">{{ detail.status }}</span>
          <span v-if="detail.原记录唯一" class="badge ok" data-testid="original-unique">原记录唯一</span>
          <button class="btn ghost" type="button" data-testid="close-detail" @click="detail = null">
            关闭详情
          </button>
        </div>
      </header>

      <dl class="recovery-grid" data-testid="detail-original">
        <div>
          <dt>失败编码编号</dt>
          <dd>{{ detail.失败编码编号 }}</dd>
        </div>
        <div>
          <dt>关联批次</dt>
          <dd>{{ detail.关联批次 }}</dd>
        </div>
        <div>
          <dt>附件 / 类型</dt>
          <dd>{{ detail.附件名 || '—' }} · {{ detail.附件类型 }}</dd>
        </div>
        <div>
          <dt>当时的编码声明</dt>
          <dd data-testid="detail-declaration">{{ detail.编码声明 }}</dd>
        </div>
        <div>
          <dt>原字节（HEX）</dt>
          <dd class="mono" data-testid="detail-raw-bytes">{{ detail.原始字节HEX || '（0 字节）' }}</dd>
        </div>
        <div>
          <dt>原错误</dt>
          <dd data-testid="detail-original-error">{{ detail.原错误 }}</dd>
        </div>
        <div>
          <dt>原转码编号</dt>
          <dd>{{ detail.原转码编号 }}</dd>
        </div>
        <div>
          <dt>读入资格</dt>
          <dd
            :class="detail.读入资格.可读入 ? 'ok-text' : 'error-text'"
            data-testid="detail-eligibility"
          >
            {{ detail.读入资格.可读入 ? '可读入' : '不可读入' }}：{{ detail.读入资格.说明 }}
          </dd>
        </div>
      </dl>

      <section class="recovery-block">
        <h5>可解码的候选（结果不同的才算真正的更正）</h5>
        <table class="data-table" data-testid="detail-candidates">
          <thead>
            <tr><th>编码</th><th>字符数</th><th>结果指纹</th><th>首行</th><th>选为更正</th></tr>
          </thead>
          <tbody>
            <tr v-for="candidate in detail.候选解码" :key="candidate.编码">
              <td>{{ candidate.编码 }}</td>
              <td>{{ candidate.字符数 }}</td>
              <td class="mono">{{ candidate.结果指纹 }}</td>
              <td class="mono">{{ candidate.首行 }}</td>
              <td>
                <button
                  class="link"
                  type="button"
                  :data-testid="`pick-candidate-${candidate.编码}`"
                  @click="correctionEncoding = candidate.编码"
                >
                  选为更正
                </button>
              </td>
            </tr>
            <tr v-if="!detail.候选解码.length">
              <td colspan="5" class="empty-state">原始字节没有可成功解码的候选编码</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section
        v-if="detail.预测字段"
        class="recovery-block"
        data-testid="detail-preview-fields"
      >
        <h5>按既有受理核对实际会进入字段解析的取值</h5>
        <p class="page-desc">{{ detail.预测字段.说明 }}</p>
        <table class="data-table">
          <thead>
            <tr>
              <th>解析行号</th>
              <th v-for="name in previewColumns" :key="name">{{ name }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in detail.预测字段.行" :key="row.解析行号">
              <td>{{ row.解析行号 }}</td>
              <td v-for="name in previewColumns" :key="name">{{ row.取值[name] ?? '—' }}</td>
            </tr>
            <tr v-if="!detail.预测字段.行.length">
              <td :colspan="previewColumns.length + 1" class="empty-state">
                该编码解不出字段行
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section v-if="detail.status !== '已恢复'" class="recovery-block" data-testid="correction-form">
        <h5>从同一附件改报编码</h5>
        <form class="analysis-form" @submit.prevent="submitCorrection">
          <label>
            <span>编码声明</span>
            <input v-model="correctionEncoding" data-testid="correction-encoding" required />
          </label>
          <label>
            <span>受理来源</span>
            <select v-model="correctionSource" data-testid="correction-source">
              <option value="manual">人工选码</option>
              <option value="channel-correction">渠道更正</option>
            </select>
          </label>
          <label>
            <span>操作者</span>
            <input v-model="operator" data-testid="correction-operator" />
          </label>
          <button class="btn primary" type="submit" data-testid="submit-correction" :disabled="busy">
            {{ busy ? '提交中…' : '提交更正' }}
          </button>
          <button
            class="btn"
            type="button"
            data-testid="retry-transcode"
            :disabled="busy"
            @click="retryCurrent"
          >
            重试转码
          </button>
        </form>
      </section>

      <section v-if="receipt" class="recovery-receipt" data-testid="recovery-receipt">
        <h5>受理回执 · {{ receipt.回执编号 }}</h5>
        <p class="receipt-conclusion" data-testid="receipt-conclusion">{{ receipt.受理结论 }}</p>
        <dl class="recovery-grid">
          <div><dt>本次受理</dt><dd data-testid="receipt-accepted">{{ receipt.本次受理来源 }} · {{ receipt.本次受理编码 }}</dd></div>
          <div><dt>本次是否采用</dt><dd data-testid="receipt-adopted">{{ receipt.本次是否采用 }}</dd></div>
          <div><dt>覆盖既有受理</dt><dd>{{ receipt.覆盖既有受理 }}<span v-if="receipt.既有受理编码">（{{ receipt.既有受理编码 }}）</span></dd></div>
          <div><dt>另发恢复</dt><dd data-testid="receipt-fork">{{ receipt.另发恢复编号 || '—' }}</dd></div>
          <div><dt>下一步</dt><dd data-testid="receipt-next-step">{{ receipt.下一步 }}</dd></div>
        </dl>
        <p v-if="receipt.下一步 === '继续核对'" class="ok-text" data-testid="receipt-hint-continue">
          按回执继续核对：确认原错误、实际字段取值与唯一原行记录。
        </p>
        <p v-else class="warn-text" data-testid="receipt-hint-fork">
          本次未采用，请按回执另发恢复 {{ receipt.另发恢复编号 }}，原记录保持不变。
        </p>
        <div class="page-actions">
          <button
            v-if="canConfirm"
            class="btn primary"
            type="button"
            data-testid="confirm-recovery"
            :disabled="busy"
            @click="confirm"
          >
            确认恢复（{{ receipt.本次受理编码 }}）
          </button>
          <button
            v-if="receipt.下一步 === '另发恢复' && receipt.另发恢复编号"
            class="btn"
            type="button"
            :data-testid="`goto-fork-${receipt.另发恢复编号}`"
            @click="openDetail(receipt.另发恢复编号)"
          >
            打开另发恢复 {{ receipt.另发恢复编号 }}
          </button>
        </div>
      </section>

      <p v-if="actionError" class="error-text" data-testid="recovery-action-error">{{ actionError }}</p>
      <p v-if="actionMessage" class="ok-text" data-testid="recovery-action-message">{{ actionMessage }}</p>

      <section class="recovery-block" data-testid="detail-attempts">
        <h5>更正与受理记录</h5>
        <table class="data-table">
          <thead>
            <tr>
              <th>受理编号</th><th>来源</th><th>编码</th><th>本次采用</th>
              <th>状态</th><th>受理结论</th><th>下一步</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="attempt in detail.更正记录" :key="attempt.受理编号">
              <td>{{ attempt.受理编号 }}</td>
              <td>{{ attempt.受理来源 }}</td>
              <td>{{ attempt.编码声明 }}</td>
              <td>{{ attempt.本次采用 }}</td>
              <td>{{ attempt.状态 }}</td>
              <td>{{ attempt.受理结论 }}</td>
              <td>{{ attempt.下一步 }}</td>
            </tr>
            <tr v-if="!detail.更正记录.length">
              <td colspan="7" class="empty-state">尚未提交任何更正</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="recovery-block" data-testid="detail-fields">
        <h5>恢复后的实际字段数据（{{ detail.恢复字段.length }}）</h5>
        <table class="data-table">
          <thead>
            <tr><th>解析行号</th><th>字段名称</th><th>原始取值</th><th>解析取值</th><th>所用编码</th></tr>
          </thead>
          <tbody>
            <tr v-for="field in detail.恢复字段" :key="field.解析编号">
              <td>{{ field.解析行号 }}</td>
              <td>{{ field.字段名称 }}</td>
              <td>{{ field.原始取值 }}</td>
              <td>{{ field.解析取值 }}</td>
              <td>{{ field.所用编码 }}</td>
            </tr>
            <tr v-if="!detail.恢复字段.length">
              <td colspan="5" class="empty-state">尚未写入字段数据</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import {
  confirmRecovery as confirmRecoveryApi,
  listRecoveries,
  loadRecoveryDetail,
  registerAttachment,
  retryTranscode as retryTranscodeApi,
  submitCorrection as submitCorrectionApi,
} from '@/api/encoding-recovery'
import type {
  RecoveryDetail,
  RecoveryListItem,
  RecoveryReceipt,
} from '@/api/encoding-recovery'

const operator = ref('operator-01')
const detail = ref<RecoveryDetail | null>(null)
const failureRows = ref<RecoveryListItem[]>([])
const receipt = ref<RecoveryReceipt | null>(null)
const correctionEncoding = ref('')
const correctionSource = ref('manual')
const busy = ref(false)
const listError = ref('')
const actionError = ref('')
const actionMessage = ref('')

const canConfirm = computed(
  () => receipt.value?.下一步 === '继续核对' && !receipt.value.已恢复,
)

const previewColumns = computed(() => {
  const first = detail.value?.预测字段?.行?.[0]
  return first ? Object.keys(first.取值) : []
})

const showAttach = ref(false)
const attachMessage = ref('')
const attachError = ref('')
const attach = ref({
  batch: '',
  attachment: '',
  bytes_hex: '',
  declared_encoding: 'unknown-charset',
  target_encoding: 'UTF-8',
})

async function submitAttachment() {
  busy.value = true
  attachMessage.value = ''
  attachError.value = ''
  try {
    const result = await registerAttachment({
      ...attach.value,
      bytes_hex: attach.value.bytes_hex.trim(),
      operator: operator.value,
    })
    attachMessage.value = `${result.附件类型}（${result.字节数} 字节）：${result.message}`
    if (result.恢复编号) {
      await openDetail(result.恢复编号)
    }
    await loadList()
  } catch (error) {
    attachError.value = error instanceof Error ? error.message : '附件登记失败'
  } finally {
    busy.value = false
  }
}

async function retryCurrent() {
  if (!detail.value) return
  busy.value = true
  actionError.value = ''
  actionMessage.value = ''
  try {
    const result = await retryTranscodeApi(
      detail.value.恢复编号,
      detail.value.候选解码[0]?.编码 ?? '',
      operator.value,
    )
    receipt.value = result.回执
    detail.value = result.details
    actionMessage.value = result.message
    await loadList()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '重试转码失败'
  } finally {
    busy.value = false
  }
}

async function loadList() {
  listError.value = ''
  try {
    failureRows.value = await listRecoveries()
  } catch (error) {
    listError.value = error instanceof Error ? error.message : '失败清单读取失败'
  }
}

async function openDetail(recoveryNo: string) {
  actionError.value = ''
  actionMessage.value = ''
  try {
    const payload = await loadRecoveryDetail(recoveryNo)
    detail.value = payload
    receipt.value = payload.受理回执
    correctionEncoding.value = payload.既有受理?.采用编码 ?? (payload.候选解码[0]?.编码 ?? '')
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '失败详情读取失败'
  }
}

async function submitCorrection() {
  if (!detail.value) return
  busy.value = true
  actionError.value = ''
  actionMessage.value = ''
  try {
    const result = await submitCorrectionApi(
      detail.value.恢复编号,
      correctionEncoding.value.trim(),
      correctionSource.value,
      operator.value,
    )
    receipt.value = result.回执
    detail.value = result.details
    actionMessage.value = result.回执.受理结论
    await loadList()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '更正受理失败'
  } finally {
    busy.value = false
  }
}

async function confirm() {
  if (!detail.value || !receipt.value) return
  busy.value = true
  actionError.value = ''
  actionMessage.value = ''
  try {
    const result = await confirmRecoveryApi(
      detail.value.恢复编号,
      receipt.value.回执编号,
      operator.value,
    )
    detail.value = result.details
    actionMessage.value = result.message
    await loadList()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '确认恢复失败'
  } finally {
    busy.value = false
  }
}

void loadList()
</script>
