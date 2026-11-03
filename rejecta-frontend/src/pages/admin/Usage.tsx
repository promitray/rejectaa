import type { ReactElement } from 'react'
import { useMemo, useState } from 'react'
import { adminApi } from '../../admin/api'
import { useLoad } from '../../admin/useLoad'
import { UsageSkeleton } from '../../components/AdminSkeletons'
import { RefreshOverlay } from '../../components/Loaders'
import { Alert, Card, PageHeader, Select } from '../../components/ui'

export default function Usage(): ReactElement {
  const [days, setDays] = useState(30)
  const report = useLoad(() => adminApi.usage(days), [days])
  const features = useLoad(() => adminApi.features())

  const names = useMemo(() => new Map((features.data ?? []).map((f) => [f.key, f.name])), [features.data])

  const byDay = useMemo(() => {
    if (!report.data?.series) return []

    const seriesMap = new Map<string, { committed: number; refunded: number }>()
    for (const point of report.data.series) {
      const day = point.day.slice(0, 10)
      const entry = seriesMap.get(day) ?? { committed: 0, refunded: 0 }
      entry.committed += point.committed
      entry.refunded += point.refunded
      seriesMap.set(day, entry)
    }

    if (seriesMap.size === 0) return []

    // Pre-populate all days in the selected range (UTC) so each day has its own bar
    const map = new Map<string, { committed: number; refunded: number }>()
    const now = new Date()
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i))
      const key = d.toISOString().slice(0, 10)
      map.set(key, seriesMap.get(key) ?? { committed: 0, refunded: 0 })
    }

    for (const [key, val] of seriesMap.entries()) {
      if (!map.has(key)) {
        map.set(key, val)
      }
    }

    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [report.data, days])

  const byFeature = useMemo(() => {
    const map = new Map<string, { committed: number; refunded: number }>()
    for (const point of report.data?.series ?? []) {
      const entry = map.get(point.feature_key) ?? { committed: 0, refunded: 0 }
      entry.committed += point.committed
      entry.refunded += point.refunded
      map.set(point.feature_key, entry)
    }
    return [...map.entries()].sort(([, a], [, b]) => b.committed - a.committed)
  }, [report.data])

  const peak = Math.max(1, ...byDay.map(([, v]) => v.committed))

  return (
    <div>
      <PageHeader
        title="Usage"
        subtitle="Completed tool runs (committed) and runs that failed and were not charged (refunded)."
        actions={
          <Select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Range" className="w-auto">
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </Select>
        }
      />
      <Alert>{report.error}</Alert>
      {report.loading && !report.data ? <UsageSkeleton /> : (
      <RefreshOverlay active={report.loading} label="Loading usage">

      <Card>
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-900">Runs per day</h2>
          {byDay.length > 0 && <span className="text-xs text-gray-500">Peak: {peak} {peak === 1 ? 'run' : 'runs'}</span>}
        </div>
        {byDay.length === 0 ? <p className="mt-3 text-sm text-gray-500">No usage in this range.</p> : (
          <>
            <div className="mt-4 flex h-40 items-end gap-1 border-b border-gray-200" role="img" aria-label="Runs per day chart">
              {byDay.map(([day, v]) => (
                <div key={day} className="group relative flex h-full flex-1 flex-col justify-end" title={`${day}: ${v.committed} runs, ${v.refunded} refunded`}>
                  <div
                    className="w-full rounded-t bg-gray-900 transition-all hover:bg-gray-700"
                    style={{
                      height: v.committed ? `${Math.max(2, (v.committed / peak) * 100)}%` : '0%',
                      minHeight: v.committed ? 2 : 0,
                    }}
                  />
                </div>
              ))}
            </div>
            <div className="mt-2 flex justify-between text-xs text-gray-400">
              <span>{byDay[0]?.[0]}</span>
              <span>{byDay[byDay.length - 1]?.[0]}</span>
            </div>
          </>
        )}
      </Card>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card>
          <h2 className="text-sm font-semibold text-gray-900">By feature</h2>
          <table className="mt-3 w-full text-sm">
            <thead className="text-left text-xs text-gray-500"><tr><th className="pb-2 font-medium">Feature</th><th className="pb-2 text-right font-medium">Runs</th><th className="pb-2 text-right font-medium">Refunded</th></tr></thead>
            <tbody className="divide-y divide-gray-100">
              {byFeature.map(([key, v]) => (
                <tr key={key}><td className="py-2">{names.get(key) ?? key}</td><td className="py-2 text-right">{v.committed}</td><td className="py-2 text-right text-gray-500">{v.refunded}</td></tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card>
          <h2 className="text-sm font-semibold text-gray-900">By plan</h2>
          <table className="mt-3 w-full text-sm">
            <thead className="text-left text-xs text-gray-500"><tr><th className="pb-2 font-medium">Plan</th><th className="pb-2 font-medium">Feature</th><th className="pb-2 text-right font-medium">Runs</th></tr></thead>
            <tbody className="divide-y divide-gray-100">
              {(report.data?.by_plan ?? []).map((r) => (
                <tr key={`${r.plan_key}-${r.feature_key}`}><td className="py-2">{r.plan_name}</td><td className="py-2">{names.get(r.feature_key) ?? r.feature_key}</td><td className="py-2 text-right">{r.committed}</td></tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
      </RefreshOverlay>
      )}
    </div>
  )
}
