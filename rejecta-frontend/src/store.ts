import { create } from 'zustand'
import type { AuthorEntry, AuthorRole } from './authors/types'
import type { AnalysisPaperResponse, Mode } from './types'

export const MAX_AUTHORS = 10

let authorCounter = 0

export function newAuthor(role: AuthorRole = 'co'): AuthorEntry {
  authorCounter += 1
  return { key: `author-${authorCounter}`, role, name: '', orcid: '', scholarId: '' }
}

interface RejectaStore {
  result: AnalysisPaperResponse | null
  loading: boolean
  error: string | null
  mode: Mode
  authors: AuthorEntry[]
  setResult: (r: AnalysisPaperResponse) => void
  setLoading: (v: boolean) => void
  setError: (e: string | null) => void
  setMode: (m: Mode) => void
  setAuthors: (authors: AuthorEntry[]) => void
  reset: () => void
}

const initialState = {
  result: null,
  loading: false,
  error: null,
  mode: 'a' as Mode,
}

export const useRejectaStore = create<RejectaStore>((set) => ({
  ...initialState,
  authors: [newAuthor('first'), newAuthor('corresponding')],
  setResult: (result) => set({ result }),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
  setMode: (mode) => set({ mode }),
  setAuthors: (authors) => set({ authors }),
  // The authors belong to the manuscript being prepared, so analysing it does not clear them.
  reset: () => set({ ...initialState }),
}))
