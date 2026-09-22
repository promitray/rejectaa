import type { AnalysisPaperResponse, Mode } from './types'

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
