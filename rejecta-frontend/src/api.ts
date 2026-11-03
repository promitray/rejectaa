import { apiFetch, notifyUsageChanged } from './auth/client'
import type {
  AbstractForgeRequest,
  AbstractForgeResponse,
  AnalysisPaperResponse,
  HeatmapAnalyzeRequest,
  HeatmapAnalyzeResponse,
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
): Promise<AnalysisPaperResponse> {
  const form = new FormData()
  form.append('file', file)
  form.append('journal', journal)
  form.append('mode', mode)
  form.append('email', email)
  return tool<AnalysisPaperResponse>('/analyse-paper', { body: form })
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
