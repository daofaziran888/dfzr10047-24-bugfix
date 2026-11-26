/** 编码恢复接口：失败详情、更正受理回执、确认恢复与重试。 */
import { request } from './client'

export type DecodeCandidate = {
  编码: string
  可解码: boolean
  字符数: number
  结果指纹: string
  首行: string
}

export type CorrectionAttempt = {
  受理编号: string
  恢复编号: string
  编码声明: string
  受理来源: string
  采用编码: string
  解码结果指纹: string
  解码结果摘要: string
  本次采用: string
  状态: string
  受理结论: string
  下一步: string
  覆盖既有受理编号?: string
  另发恢复编号?: string
  回执编号?: string
  操作者: string
  受理时间: string
}

export type RecoveryReceipt = {
  回执编号: string
  恢复编号: string
  失败编码编号: string
  关联批次: string
  本次受理编号: string
  本次受理来源: string
  本次受理编码: string
  本次是否采用: string
  覆盖既有受理: string
  既有受理编号: string
  既有受理编码: string
  另发恢复编号: string
  下一步: string
  受理结论: string
  原错误: string
  编码声明: string
  原始字节HEX: string
  已恢复: boolean
}

export type PreviewRow = {
  解析行号: string
  取值: Record<string, string>
}

export type PreviewFields = {
  编码: string
  可解码: boolean
  说明: string
  字段数?: number
  行: PreviewRow[]
}

export type RecoveredField = {
  解析编号: string
  解析行号: string
  字段名称: string
  原始取值: string
  解析取值: string
  所用编码: string
}

export type ReadEligibility = {
  可读入: boolean
  说明: string
  关联批次: string
  转码记录数: number
  失败编码记录数: number
  未恢复失败记录: string[]
  已核定转码编号: string
  核定标记: string
}

export type RecoveryDetail = {
  恢复编号: string
  失败编码编号: string
  关联批次: string
  附件名: string
  附件类型: string
  记录类型: string
  编码声明: string
  历史声明: string
  目标编码: string
  替换策略: string
  原始字节HEX: string
  原始字节数: string
  原错误: string
  原转码编号: string
  status: string
  候选解码: DecodeCandidate[]
  更正记录: CorrectionAttempt[]
  既有受理: CorrectionAttempt | null
  受理回执: RecoveryReceipt | null
  恢复编码: string
  恢复字段数: string
  恢复字段: RecoveredField[]
  预测字段: PreviewFields | null
  原记录唯一: boolean
  另发恢复编号: string
  父恢复编号: string
  读入资格: ReadEligibility
}

export type CorrectionResult = {
  ok: boolean
  message: string
  回执: RecoveryReceipt
  另发恢复编号?: string
  details: RecoveryDetail
}

export type ConfirmResult = {
  ok: boolean
  message: string
  field_count?: number
  duplicated?: boolean
  details: RecoveryDetail
}

export type AttachmentResult = {
  ok: boolean
  message: string
  附件类型: string
  字节数: string
  恢复编号?: string
  失败编码编号?: string
  失败详情?: RecoveryDetail
  entry?: Record<string, unknown> | null
}

async function post<T>(path: string, body: unknown, fallback: string): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  if (!response.ok) throw new Error(`${fallback}（接口返回 ${response.status}）`)
  return (await response.json()) as T
}

export type RecoveryListItem = {
  id: number
  status: string
  恢复编号: string
  失败编码编号: string
  关联批次: string
  附件名: string
  编码声明: string
  原始编码: string
  记录类型: string
  原错误: string
  恢复编码: string
  原始字节数: string
  父恢复编号: string
  另发恢复编号: string
}

export async function listRecoveries(): Promise<RecoveryListItem[]> {
  const response = await request('/api/encoding-recovery/failures?size=200')
  if (!response.ok) throw new Error('失败编码记录读取失败')
  const payload = (await response.json()) as { items: RecoveryListItem[] }
  return payload.items ?? []
}

export async function loadRecoveryDetail(recoveryNo: string): Promise<RecoveryDetail> {
  const response = await request(
    `/api/encoding-recovery/failures/${encodeURIComponent(recoveryNo)}`,
  )
  if (!response.ok) throw new Error(`恢复单 ${recoveryNo} 读取失败`)
  return (await response.json()) as RecoveryDetail
}

export async function submitCorrection(
  recoveryNo: string,
  encoding: string,
  source: string,
  operator: string,
): Promise<CorrectionResult> {
  return post<CorrectionResult>(
    `/api/encoding-recovery/failures/${encodeURIComponent(recoveryNo)}/corrections`,
    { encoding, source, operator },
    '更正受理失败',
  )
}

export async function confirmRecovery(
  recoveryNo: string,
  receiptNo: string,
  operator: string,
): Promise<ConfirmResult> {
  return post<ConfirmResult>(
    `/api/encoding-recovery/failures/${encodeURIComponent(recoveryNo)}/confirm`,
    { receipt_no: receiptNo, operator },
    '确认恢复失败',
  )
}

export async function retryTranscode(
  recoveryNo: string,
  encoding: string,
  operator: string,
): Promise<CorrectionResult> {
  return post<CorrectionResult>(
    `/api/encoding-recovery/failures/${encodeURIComponent(recoveryNo)}/retry`,
    { encoding, operator },
    '重试转码失败',
  )
}

export async function registerAttachment(values: {
  batch: string
  attachment: string
  bytes_hex: string
  declared_encoding: string
  target_encoding: string
  operator: string
}): Promise<AttachmentResult> {
  return post<AttachmentResult>('/api/encoding-recovery/attachments', values, '附件登记失败')
}
