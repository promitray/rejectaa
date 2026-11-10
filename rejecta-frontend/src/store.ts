import { create } from 'zustand'
import type { AuthorEntry, AuthorRole } from './authors/types'
import type { AnalysisPaperResponse, ArticleType, DocumentInfo, Mode } from './types'

export const MAX_AUTHORS = 10

let authorCounter = 0

export function newAuthor(role: AuthorRole = 'co'): AuthorEntry {
  authorCounter += 1
  return { key: `author-${authorCounter}`, role, name: '', orcid: '', scholarId: '' }
}

/** The last upload, kept in memory only, so "Run again" can send the same file again. */
export interface LastSubmission {
  file: File
  journal: string
  mode: Mode
  email: string
  articleType: ArticleType | 'auto'
  keepCopy: boolean
}

interface RejectaStore {
  result: AnalysisPaperResponse | null
  lastSubmission: LastSubmission | null
  setLastSubmission: (s: LastSubmission | null) => void
  loading: boolean
  error: string | null
  mode: Mode
  authors: AuthorEntry[]
  setResult: (r: AnalysisPaperResponse) => void
  /** Replace the working-copy info after the server changed the paper (apply, undo) or null after delete. */
  setDocument: (d: DocumentInfo | null) => void
  setLoading: (v: boolean) => void
  setError: (e: string | null) => void
  setMode: (m: Mode) => void
  setAuthors: (authors: AuthorEntry[]) => void
  reset: () => void
}

const initialState = {
  result: null,
  lastSubmission: null,
  loading: false,
  error: null,
  mode: 'a' as Mode,
}

export const useRejectaStore = create<RejectaStore>((set) => ({
  ...initialState,
  authors: [newAuthor('first'), newAuthor('corresponding')],
  setResult: (result) => set({ result }),
  setLastSubmission: (lastSubmission) => set({ lastSubmission }),
  setDocument: (document) =>
    set((state) => (state.result ? { result: { ...state.result, document } } : {})),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
  setMode: (mode) => set({ mode }),
  setAuthors: (authors) => set({ authors }),
  // The authors belong to the manuscript being prepared, so analysing it does not clear them.
  reset: () => set({ ...initialState }),
}))
