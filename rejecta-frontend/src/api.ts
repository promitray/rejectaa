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
const BASE = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

function errorMessage(body: unknown, status: number): string {
  if (typeof body !== 'object' || body === null || !('detail' in body)) {
    return `Request failed: ${status}`
  }

  const detail = (body as { detail: unknown }).detail
  if (typeof detail === 'string' && detail.length > 0) {
    return detail
  }

  if (Array.isArray(detail)) {
    const messages = detail.flatMap((item) => {
      if (typeof item === 'object' && item !== null && 'msg' in item) {
        const msg = (item as { msg: unknown }).msg
        return typeof msg === 'string' ? [msg] : []
      }
      return []
    })
    if (messages.length > 0) {
      return messages.join('; ')
    }
  }

  return `Request failed: ${status}`
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

  const res = await fetch(`${BASE}/analyse-paper`, {
    method: 'POST',
    body: form,
  })

  if (!res.ok) {
    const err: unknown = await res.json().catch(() => ({}))
    throw new Error(errorMessage(err, res.status))
  }

  return res.json() as Promise<AnalysisPaperResponse>
}

export async function calibrateVoice(
  req: VoiceCalibrateRequest,
): Promise<VoiceCalibrateResponse> {
  const res = await fetch(`${BASE}/voice-calibrate`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(req),
  })

  if (!res.ok) {
    const err: unknown = await res.json().catch(() => ({}))
    throw new Error(errorMessage(err, res.status))
  }

  return res.json() as Promise<VoiceCalibrateResponse>
}

export async function analyzeHeatmap(
  req: HeatmapAnalyzeRequest,
): Promise<HeatmapAnalyzeResponse> {
  const res = await fetch(`${BASE}/ai-signal-heatmap/analyze`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(req),
  })

  if (!res.ok) {
    const err: unknown = await res.json().catch(() => ({}))
    throw new Error(errorMessage(err, res.status))
  }

  return res.json() as Promise<HeatmapAnalyzeResponse>
}

export async function forgeAbstract(
  req: AbstractForgeRequest,
): Promise<AbstractForgeResponse> {
  const res = await fetch(`${BASE}/abstract-forge/generate`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(req),
  })

  if (!res.ok) {
    const err: unknown = await res.json().catch(() => ({}))
    throw new Error(errorMessage(err, res.status))
  }

  return res.json() as Promise<AbstractForgeResponse>
}

export async function analyzeRebuttal(
  req: RebuttalAnalyzeRequest,
): Promise<RebuttalAnalyzeResponse> {
  const res = await fetch(`${BASE}/rebuttal-writer/analyze`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(req),
  })

  if (!res.ok) {
    const err: unknown = await res.json().catch(() => ({}))
    throw new Error(errorMessage(err, res.status))
  }

  return res.json() as Promise<RebuttalAnalyzeResponse>
}
