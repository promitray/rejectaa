/** Skeletons for the admin console, each shaped like the page it stands in for. */
import type { ReactElement, ReactNode } from 'react'
import { Delayed, GlassTile, LoadingRegion, SkeletonBlock, SkeletonLine } from './Loaders'

function Wrap({ label, children }: { label: string; children: ReactNode }): ReactElement {
  return (
    <Delayed>
      <LoadingRegion label={label}>{children}</LoadingRegion>
    </Delayed>
  )
}

function Heading(): ReactElement {
  return (
    <div className="mb-6 space-y-2">
      <SkeletonBlock className="h-6 w-48" />
      <SkeletonLine className="w-80 max-w-full" />
    </div>
  )
}

export function OverviewSkeleton(): ReactElement {
  return (
    <Wrap label="Loading overview">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {Array.from({ length: 8 }, (_, i) => (
          <GlassTile key={i}>
            <SkeletonLine className="w-20" />
            <SkeletonBlock className="mt-3 h-7 w-16" />
          </GlassTile>
        ))}
      </div>
      <GlassTile className="mt-6">
        <SkeletonLine className="w-32" />
        <div className="mt-4 space-y-3">
          {Array.from({ length: 4 }, (_, i) => (
            <div key={i} className="flex justify-between">
              <SkeletonLine className="w-28" />
              <SkeletonLine className="w-8" />
            </div>
          ))}
        </div>
      </GlassTile>
    </Wrap>
  )
}

export function TableSkeleton({ rows = 6, label = 'Loading' }: { rows?: number; label?: string }): ReactElement {
  return (
    <Wrap label={label}>
      <div className="glass overflow-hidden rounded-xl">
        <div className="flex gap-6 border-b border-gray-200/70 px-4 py-3">
          {Array.from({ length: 5 }, (_, i) => (
            <SkeletonLine key={i} className="h-2.5 w-16" />
          ))}
        </div>
        <div className="divide-y divide-gray-100/80">
          {Array.from({ length: rows }, (_, i) => (
            <div key={i} className="flex items-center gap-6 px-4 py-3.5">
              <div className="min-w-0 flex-[2] space-y-1.5">
                <SkeletonLine className="w-3/5" />
                <SkeletonLine className="h-2.5 w-2/5" />
              </div>
              <SkeletonLine className="flex-1" />
              <SkeletonBlock className="h-5 w-14 rounded-full" />
              <SkeletonBlock className="h-5 w-16 rounded-full" />
              <SkeletonLine className="hidden flex-1 sm:block" />
            </div>
          ))}
        </div>
      </div>
    </Wrap>
  )
}

export function CardGridSkeleton({ count = 6, label = 'Loading' }: { count?: number; label?: string }): ReactElement {
  return (
    <Wrap label={label}>
      <div className="grid gap-4 md:grid-cols-2">
        {Array.from({ length: count }, (_, i) => (
          <GlassTile key={i}>
            <div className="flex items-start justify-between">
              <div className="space-y-2">
                <SkeletonBlock className="h-5 w-28" />
                <SkeletonLine className="h-2.5 w-16" />
              </div>
              <SkeletonBlock className="h-5 w-14 rounded-full" />
            </div>
            <SkeletonLine className="mt-4 w-4/5" />
            <div className="mt-4 space-y-2.5">
              {Array.from({ length: 5 }, (_, j) => (
                <div key={j} className="flex justify-between gap-6">
                  <SkeletonLine className="h-2.5 w-24" />
                  <SkeletonLine className="h-2.5 w-32" />
                </div>
              ))}
            </div>
          </GlassTile>
        ))}
      </div>
    </Wrap>
  )
}

export function ListSkeleton({ count = 4, label = 'Loading' }: { count?: number; label?: string }): ReactElement {
  return (
    <Wrap label={label}>
      <div className="flex flex-col gap-3">
        {Array.from({ length: count }, (_, i) => (
          <GlassTile key={i} className="flex items-center justify-between gap-4">
            <div className="min-w-0 flex-1 space-y-2">
              <SkeletonBlock className="h-4 w-40" />
              <SkeletonLine className="h-2.5 w-28" />
              <SkeletonLine className="w-3/4" />
            </div>
            <SkeletonBlock className="h-5 w-10 rounded-full" />
          </GlassTile>
        ))}
      </div>
    </Wrap>
  )
}

/** User detail and partner detail: title bar plus two columns of cards. */
export function DetailSkeleton({ label = 'Loading' }: { label?: string }): ReactElement {
  return (
    <Wrap label={label}>
      <div className="mb-6 space-y-2">
        <SkeletonLine className="h-2.5 w-16" />
        <SkeletonBlock className="h-6 w-64 max-w-full" />
        <SkeletonLine className="w-72 max-w-full" />
      </div>
      <div className="grid gap-6 lg:grid-cols-[1fr_1.6fr]">
        <div className="flex flex-col gap-6">
          <GlassTile>
            <SkeletonLine className="w-32" />
            <div className="mt-4 space-y-4">
              {Array.from({ length: 4 }, (_, i) => (
                <div key={i} className="space-y-2">
                  <SkeletonLine className="h-2.5 w-16" />
                  <SkeletonBlock className="h-9 w-full" />
                </div>
              ))}
            </div>
          </GlassTile>
          <GlassTile>
            <SkeletonLine className="w-24" />
            <div className="mt-4 space-y-2.5">
              <SkeletonLine className="w-full" />
              <SkeletonLine className="w-5/6" />
            </div>
          </GlassTile>
        </div>
        <GlassTile>
          <SkeletonLine className="w-40" />
          <div className="mt-5 space-y-5">
            {Array.from({ length: 5 }, (_, i) => (
              <div key={i} className="flex items-center justify-between gap-4">
                <div className="space-y-2">
                  <SkeletonLine className="w-28" />
                  <SkeletonLine className="h-2.5 w-12" />
                </div>
                <SkeletonBlock className="h-2 w-24" />
                <SkeletonBlock className="h-2 w-24" />
                <SkeletonBlock className="h-8 w-20" />
              </div>
            ))}
          </div>
        </GlassTile>
      </div>
    </Wrap>
  )
}

export function PlanEditorSkeleton(): ReactElement {
  return (
    <Wrap label="Loading plan">
      <div className="mb-6 flex items-start justify-between gap-3">
        <div className="space-y-2">
          <SkeletonLine className="h-2.5 w-12" />
          <SkeletonBlock className="h-6 w-40" />
          <SkeletonLine className="w-64" />
        </div>
        <div className="flex gap-2">
          <SkeletonBlock className="h-9 w-24" />
          <SkeletonBlock className="h-9 w-28" />
        </div>
      </div>
      <GlassTile>
        <div className="flex items-center justify-between">
          <SkeletonLine className="w-36" />
          <SkeletonBlock className="h-9 w-32" />
        </div>
        <div className="mt-6 space-y-5">
          {Array.from({ length: 5 }, (_, i) => (
            <div key={i} className="flex items-center gap-6">
              <SkeletonLine className="w-32" />
              <SkeletonBlock className="h-5 w-9 rounded-full" />
              <SkeletonBlock className="h-9 w-24" />
              <SkeletonBlock className="h-9 w-24" />
            </div>
          ))}
        </div>
      </GlassTile>
      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        {[0, 1].map((i) => (
          <GlassTile key={i}>
            <SkeletonLine className="w-24" />
            <div className="mt-4 space-y-3">
              <SkeletonBlock className="h-9 w-full" />
              <SkeletonBlock className="h-9 w-full" />
            </div>
          </GlassTile>
        ))}
      </div>
    </Wrap>
  )
}

export function UsageSkeleton(): ReactElement {
  return (
    <Wrap label="Loading usage">
      <GlassTile>
        <SkeletonLine className="w-28" />
        <div className="mt-5 flex h-40 items-end gap-1">
          {[40, 65, 30, 80, 55, 90, 45, 70, 35, 60, 85, 50, 75, 42, 68, 38, 82, 58, 72, 48].map((h, i) => (
            <div key={i} className="flex h-full flex-1 items-end">
              <div className="skeleton w-full rounded-b-none" style={{ height: `${h}%` }} aria-hidden="true" />
            </div>
          ))}
        </div>
      </GlassTile>
      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        {[0, 1].map((i) => (
          <GlassTile key={i}>
            <SkeletonLine className="w-20" />
            <div className="mt-4 space-y-3">
              {Array.from({ length: 4 }, (_, j) => (
                <SkeletonLine key={j} className="w-full" />
              ))}
            </div>
          </GlassTile>
        ))}
      </div>
    </Wrap>
  )
}

export function SectionSkeleton(): ReactElement {
  return (
    <Wrap label="Loading">
      <Heading />
      <div className="grid gap-4 md:grid-cols-2">
        {[0, 1].map((i) => (
          <GlassTile key={i}>
            <SkeletonLine className="w-1/3" />
            <div className="mt-4 space-y-2.5">
              <SkeletonLine className="w-full" />
              <SkeletonLine className="w-5/6" />
              <SkeletonLine className="w-2/3" />
            </div>
          </GlassTile>
        ))}
      </div>
    </Wrap>
  )
}
