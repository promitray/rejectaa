/** ORCID and Google Scholar identifiers. Same ORCID rules as the API, so typos never reach it. */

const ORCID_SHAPE = /^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$/

function checkDigit(first15: string): string {
  let total = 0
  for (const char of first15) {
    total = (total + Number(char)) * 2
  }
  const result = (12 - (total % 11)) % 11
  return result === 10 ? 'X' : String(result)
}

/** `0000-0002-1825-0097` from a bare id, a URL, or digits without dashes. Null if it is not a valid ORCID. */
export function normaliseOrcid(raw: string): string | null {
  const afterHost = raw.trim().split(/orcid\.org\//i).pop() ?? ''
  const text = afterHost.trim().replace(/\/+$/, '').toUpperCase()
  const compact = text.replace(/[-\s]/g, '')
  const dashed = compact.length === 16 ? compact.replace(/(.{4})(?=.)/g, '$1-') : text
  if (!ORCID_SHAPE.test(dashed)) {
    return null
  }
  const digits = dashed.replace(/-/g, '')
  return checkDigit(digits.slice(0, 15)) === digits[15] ? dashed : null
}

export function orcidUrl(orcid: string): string {
  return `https://orcid.org/${orcid}`
}

const SCHOLAR_ID = /^[A-Za-z0-9_-]{12}$/

/** The 12-character Scholar id from a bare id or a profile URL (`...citations?user=ID`). Null if invalid. */
export function normaliseScholarId(raw: string): string | null {
  const text = raw.trim()
  const fromUrl = /[?&]user=([^&#\s]+)/.exec(text)?.[1]
  const candidate = fromUrl ?? text
  return SCHOLAR_ID.test(candidate) ? candidate : null
}

export function scholarUrl(id: string): string {
  return `https://scholar.google.com/citations?user=${id}`
}
