/**
 * Does the name the user typed belong to the person behind the ORCID?
 *
 * Only a warning signal, never a block: names are written many ways
 * ("J. Priem", "Priem, Jason", "José" / "Jose"), and in some languages the
 * order differs. So the order is ignored and initials count as a match.
 */

export type NameCheck = 'match' | 'mismatch' | 'unknown'

const TITLES = new Set(['dr', 'prof', 'professor', 'mr', 'mrs', 'ms', 'md', 'phd', 'mph', 'msc', 'jr', 'sr'])

function tokens(name: string): string[] {
  return name
    .normalize('NFD')
    .replace(/\p{M}/gu, '') // accents: José -> Jose
    .toLowerCase()
    .split(/[^\p{L}]+/u)
    .filter((token) => token.length > 0 && !TITLES.has(token))
}

/** Equal words, or an initial against a word that starts with it ("j" and "jason"). */
function sameWord(a: string, b: string): boolean {
  if (a === b) return true
  if (a.length === 1) return b.startsWith(a)
  if (b.length === 1) return a.startsWith(b)
  return false
}

export function compareNames(typed: string, official: string | null | undefined): NameCheck {
  const mine = tokens(typed)
  const theirs = tokens(official ?? '')
  if (mine.length === 0 || theirs.length === 0) {
    return 'unknown'
  }
  const matched = mine.filter((word) => theirs.some((other) => sameWord(word, other))).length
  const fullWordMatch = mine.some((word) => word.length > 1 && theirs.includes(word))
  // Two words must agree (one if only one was typed); extra middle names are tolerated.
  const enough = matched >= Math.min(2, mine.length)
  return enough && fullWordMatch ? 'match' : 'mismatch'
}
