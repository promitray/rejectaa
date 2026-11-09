import { apiFetch, notifyUsageChanged } from './auth/client'
import type {
  AbstractForgeRequest,
  AbstractForgeResponse,
  AnalysisPaperResponse,
  ApplyResult,
  DocumentInfo,
  DocumentProposal,
  HeatmapAnalyzeRequest,
  HeatmapAnalyzeResponse,
  ArticleType,
  Mode,
  RebuttalAnalyzeRequest,
  RebuttalAnalyzeResponse,
  VoiceCalibrateRequest,
  VoiceCalibrateResponse,
} from './types'

export { ApiError } from './auth/client'

/** Tool calls need a signed-in user and use the plan's quota. */
async function tool<T>(path: string, init: RequestInit): Promise<T> {
  try {
    return await apiFetch<T>(path, { method: 'POST', ...init })
  } finally {
    notifyUsageChanged()
  }
}

function json(req: unknown): RequestInit {
  return { body: JSON.stringify(req), headers: { 'Content-Type': 'application/json' } }
}

export async function submitPaper(
  file: File,
  journal: string,
  mode: Mode,
  email: string,
  articleType: ArticleType | 'auto' = 'auto',
  keepCopy = false,
): Promise<AnalysisPaperResponse> {
  const form = new FormData()
  form.append('file', file)
  form.append('journal', journal)
  form.append('mode', mode)
  form.append('email', email)
  form.append('article_type', articleType)
  form.append('keep_copy', keepCopy ? 'true' : 'false')
  return tool<AnalysisPaperResponse>('/analyse-paper', { body: form })
}

// --- working copy of the paper (all logic is on the server; these only call it) ---

export interface DocumentCalibrateBody {
  journal: string
  field?: string
  intensity?: 'polish' | 'rewrite' | 'restructure'
  focus?: string
  from_paragraph?: number
}

export function calibrateDocumentSection(
  documentId: string,
  sectionId: string,
  body: DocumentCalibrateBody,
): Promise<DocumentProposal> {
  return tool<DocumentProposal>(
    `/documents/${encodeURIComponent(documentId)}/sections/${encodeURIComponent(sectionId)}/calibrate`,
    json(body),
  )
}

export function applyProposal(documentId: string, proposalId: string): Promise<ApplyResult> {
  return apiFetch<ApplyResult>(
    `/documents/${encodeURIComponent(documentId)}/proposals/${encodeURIComponent(proposalId)}/apply`,
    { method: 'POST' },
  )
}

export function rejectProposal(documentId: string, proposalId: string): Promise<void> {
  return apiFetch<void>(
    `/documents/${encodeURIComponent(documentId)}/proposals/${encodeURIComponent(proposalId)}/reject`,
    { method: 'POST' },
  )
}

export function undoDocument(documentId: string): Promise<DocumentInfo> {
  return apiFetch<DocumentInfo>(`/documents/${encodeURIComponent(documentId)}/undo`, { method: 'POST' })
}

export function deleteDocument(documentId: string): Promise<void> {
  return apiFetch<void>(`/documents/${encodeURIComponent(documentId)}`, { method: 'DELETE' })
}

/** Download the current version of the paper as a Word file. */
export async function downloadDocument(documentId: string, filename: string): Promise<void> {
  const blob = await apiFetch<Blob>(`/documents/${encodeURIComponent(documentId)}/download`, {}, { blob: true })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

export function calibrateVoice(req: VoiceCalibrateRequest): Promise<VoiceCalibrateResponse> {
  return tool<VoiceCalibrateResponse>('/voice-calibrate', json(req))
}

export function analyzeHeatmap(req: HeatmapAnalyzeRequest): Promise<HeatmapAnalyzeResponse> {
  return tool<HeatmapAnalyzeResponse>('/ai-signal-heatmap/analyze', json(req))
}

export function forgeAbstract(req: AbstractForgeRequest): Promise<AbstractForgeResponse> {
  return tool<AbstractForgeResponse>('/abstract-forge/generate', json(req))
}

export function analyzeRebuttal(req: RebuttalAnalyzeRequest): Promise<RebuttalAnalyzeResponse> {
  return tool<RebuttalAnalyzeResponse>('/rebuttal-writer/analyze', json(req))
}
