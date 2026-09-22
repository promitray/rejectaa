import { create } from 'zustand'
import type { AnalysisPaperResponse, Mode } from './types'

interface RejectaStore {
  result: AnalysisPaperResponse | null
  loading: boolean
  error: string | null
  mode: Mode
  setResult: (r: AnalysisPaperResponse) => void
  setLoading: (v: boolean) => void
  setError: (e: string | null) => void
  setMode: (m: Mode) => void
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
  setResult: (result) => set({ result }),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
  setMode: (mode) => set({ mode }),
  reset: () => set(initialState),
}))
