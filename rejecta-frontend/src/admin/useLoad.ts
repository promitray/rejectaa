import { useCallback, useEffect, useState } from 'react'

/** Load data for an admin page; `reload()` re-runs the loader. */
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []): {
  data: T | null
  error: string | null
  loading: boolean
  reload: () => Promise<void>
  setData: (value: T) => void
} {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(loader, deps)

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await run())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load data.')
    } finally {
      setLoading(false)
    }
  }, [run])

  useEffect(() => {
    void reload()
  }, [reload])

  return { data, error, loading, reload, setData }
}

/** Run an admin action, collecting its error message. */
export async function attempt(action: () => Promise<unknown>, onError: (message: string) => void): Promise<boolean> {
  try {
    await action()
    return true
  } catch (err) {
    onError(err instanceof Error ? err.message : 'Action failed.')
    return false
  }
}
