import type { AuthorProfile, AuthorWork, VenueInfo } from './types'

export interface YearPoint {
  year: number
  papers: number
  citations: number
}

export interface Slice {
  name: string
  value: number
}

export interface Aggregate {
  /** Papers behind the charts (the profile's fetched works, shared papers counted once). */
  papers: number
  citations: number
  perYear: YearPoint[]
  types: Slice[]
  journals: VenueInfo[]
  otherVenues: VenueInfo[]
}

const TYPE_LABEL: Record<string, string> = {
  article: 'Journal article',
  review: 'Review',
  preprint: 'Preprint',
  'book-chapter': 'Book chapter',
  book: 'Book',
  dataset: 'Dataset',
  dissertation: 'Thesis',
  letter: 'Letter',
  editorial: 'Editorial',
}
const MAX_TYPE_SLICES = 5
const MAX_YEARS = 20
const MAX_JOURNALS = 8
const MAX_OTHER_VENUES = 3

export function typeLabel(type: string): string {
  return TYPE_LABEL[type] ?? 'Other'
}

/** Papers shared by several authors are counted once. */
export function uniqueWorks(profiles: AuthorProfile[]): AuthorWork[] {
  const seen = new Map<string, AuthorWork>()
  for (const profile of profiles) {
    for (const work of profile.works) {
      if (!seen.has(work.id)) {
        seen.set(work.id, work)
      }
    }
  }
  return [...seen.values()]
}

export function aggregate(profiles: AuthorProfile[]): Aggregate {
  const works = uniqueWorks(profiles)

  const byYear = new Map<number, YearPoint>()
  for (const work of works) {
    if (work.year === null) continue
    const point = byYear.get(work.year) ?? { year: work.year, papers: 0, citations: 0 }
    point.papers += 1
    point.citations += work.cites
    byYear.set(work.year, point)
  }
  const years = [...byYear.keys()]
  const last = years.length ? Math.max(...years) : 0
  const first = years.length ? Math.max(Math.min(...years), last - (MAX_YEARS - 1)) : 0
  const perYear: YearPoint[] = []
  for (let year = first; year <= last && years.length; year += 1) {
    perYear.push(byYear.get(year) ?? { year, papers: 0, citations: 0 })
  }

  const typeCounts = new Map<string, number>()
  for (const work of works) {
    const label = typeLabel(work.type)
    typeCounts.set(label, (typeCounts.get(label) ?? 0) + 1)
  }
  const sorted = [...typeCounts.entries()].sort((a, b) => b[1] - a[1])
  const kept = sorted.filter(([name]) => name !== 'Other').slice(0, MAX_TYPE_SLICES)
  const keptTotal = kept.reduce((sum, [, count]) => sum + count, 0)
  const types: Slice[] = kept.map(([name, value]) => ({ name, value }))
  if (works.length > keptTotal) {
    types.push({ name: 'Other', value: works.length - keptTotal })
  }

  // Venue names, kind and impact come from the profiles; the counts are recomputed over unique papers.
  const venueInfo = new Map<string, VenueInfo>()
  for (const profile of profiles) {
    for (const venue of profile.venues) {
      if (!venueInfo.has(venue.id)) venueInfo.set(venue.id, venue)
    }
  }
  const venueCounts = new Map<string, number>()
  for (const work of works) {
    if (work.venue_id && venueInfo.has(work.venue_id)) {
      venueCounts.set(work.venue_id, (venueCounts.get(work.venue_id) ?? 0) + 1)
    }
  }
  const venues = [...venueCounts.entries()]
    .map(([id, count]) => ({ ...(venueInfo.get(id) as VenueInfo), works: count }))
    .sort((a, b) => b.works - a.works)

  return {
    papers: works.length,
    citations: works.reduce((sum, work) => sum + work.cites, 0),
    perYear,
    types,
    journals: venues.filter((v) => v.kind === 'journal').slice(0, MAX_JOURNALS),
    otherVenues: venues.filter((v) => v.kind !== 'journal').slice(0, MAX_OTHER_VENUES),
  }
}

/** "Zenodo (CERN European Organization for Nuclear Research)" -> "Zenodo". */
export function shortVenueName(name: string, max = 30): string {
  const base = name.replace(/\s*\(.*\)\s*$/, '').trim() || name
  return base.length > max ? `${base.slice(0, max - 1)}…` : base
}
