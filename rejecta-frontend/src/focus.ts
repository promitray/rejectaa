import type { FixStep } from './types'

/** The most characters of editor's note the API accepts for a section (models.MAX_FOCUS_CHARS). */
export const MAX_FOCUS_CHARS = 4000
const MIN_QUOTE_CHARS = 90
/** What the note says of an issue about something the text lacks; the server's Calibrator looks for these words (engine.MISSING_MARK). */
export const MISSING_MARK = 'missing from the text'

/**
 * The editor's note a section is calibrated with: every issue found in it, the words it is about, and how to fix
 * it, most important first. The Calibrator is told to resolve each one and to report each by its number.
 *
 * Steps about words outside the text Calibrate receives are left out (`inCalibrateText` false). When the note is
 * too long the quotations are shortened first, then the least important steps are dropped.
 */
export function buildSectionFocus(section: string, steps: FixStep[]): { note: string; used: FixStep[]; left: FixStep[] } {
  const used = steps.filter((step) => step.in_calibrate_text !== false)
  const left = steps.filter((step) => step.in_calibrate_text === false)
  const render = (list: FixStep[], quoteChars: number): string => {
    const lines = [
      `Section: ${section}.`,
      'Issues to resolve in this section, most important first. Each has the words it is about and how to fix it:',
    ]
    list.forEach((step, index) => {
      // an issue about something the text lacks is marked, so the Calibrator puts a placeholder where it belongs
      lines.push(`${index + 1}. [${step.severity}${step.kind === 'add' ? `, ${MISSING_MARK}` : ''}] ${step.problem}`)
      if (step.quote) lines.push(`   In the text: “${step.quote.length > quoteChars ? `${step.quote.slice(0, quoteChars).trimEnd()} …` : step.quote}”`)
      lines.push(`   Fix: ${step.action}`)
    })
    return lines.join('\n')
  }
  let kept = used
  for (const quoteChars of [400, 200, MIN_QUOTE_CHARS]) {
    const note = render(kept, quoteChars)
    if (note.length <= MAX_FOCUS_CHARS) return { note, used: kept, left: [...left, ...used.slice(kept.length)] }
  }
  while (kept.length > 1 && render(kept, MIN_QUOTE_CHARS).length > MAX_FOCUS_CHARS) kept = kept.slice(0, -1)
  return { note: render(kept, MIN_QUOTE_CHARS).slice(0, MAX_FOCUS_CHARS), used: kept, left: [...left, ...used.slice(kept.length)] }
}
