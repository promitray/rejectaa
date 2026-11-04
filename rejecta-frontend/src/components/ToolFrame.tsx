import type { ReactElement, ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../auth/AuthProvider'
import type { FeatureKey, FeatureUsage } from '../auth/types'

function summary(f: FeatureUsage): { text: string; tone: 'normal' | 'low' | 'out' } {
  if (!f.enabled) {
    return {
      text: f.reason === 'feature_disabled' ? `${f.name} is temporarily unavailable.` : `${f.name} is not included in your plan.`,
      tone: 'out',
    }
  }
  const parts: string[] = []
  let tone: 'normal' | 'low' | 'out' = 'normal'
  for (const [left, limit, label] of [
    [f.remaining_today, f.daily_limit, 'today'],
    [f.remaining_this_month, f.monthly_limit, 'this month'],
  ] as const) {
    if (limit === null || left === null) continue
    parts.push(`${left} of ${limit} left ${label}`)
    if (left === 0) tone = 'out'
    else if (left <= Math.max(1, Math.floor(limit * 0.2)) && tone !== 'out') tone = 'low'
  }
  return { text: parts.length ? parts.join(' · ') : 'Unlimited on your plan', tone }
}

/** Thin usage strip above a tool page, so people see their limit before they run it. */
export default function ToolFrame({ feature, children }: { feature: FeatureKey; children: ReactNode }): ReactElement {
  const { me } = useAuth()
  const usage = me?.features.find((f) => f.key === feature)
  const info = usage ? summary(usage) : null
  const tones = {
    normal: 'border-gray-200 bg-gray-50 text-gray-600',
    low: 'border-amber-200 bg-amber-50 text-amber-900',
    out: 'border-red-200 bg-red-50 text-red-800',
  }
  return (
    <>
      {info ? (
        <div className={`border-b ${tones[info.tone]} print:hidden`}>
          <div className="mx-auto flex max-w-5xl items-center justify-between gap-3 px-4 py-2 text-xs">
            <span>
              <span className="font-medium">{usage?.name}</span> · {info.text}
            </span>
            <Link to="/account" className="shrink-0 underline-offset-2 hover:underline">
              {me?.plan?.name ?? ''} plan
            </Link>
          </div>
        </div>
      ) : null}
      {children}
    </>
  )
}
