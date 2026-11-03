/**
 * Loading states: frosted-glass tiles and shimmering skeletons.
 *
 * - Initial loads show a skeleton shaped like the real content (no layout jump).
 * - Reloads keep the old content, blurred, with a small "Updating…" pill.
 * - Long model calls show a glass "working" tile with rotating status text.
 * - Nothing appears for the first 150 ms, so fast loads never flicker.
 * - Motion stops for people who ask for reduced motion (see index.css).
 */
import type { ReactElement, ReactNode } from 'react'
import { useEffect, useState } from 'react'

const SHOW_AFTER_MS = 150

/** True once `active` has stayed true for `delay` ms. */
export function useDelayedFlag(active: boolean, delay = SHOW_AFTER_MS): boolean {
  const [on, setOn] = useState(false)
  useEffect(() => {
    if (!active) {
      setOn(false)
      return
    }
    const timer = window.setTimeout(() => setOn(true), delay)
    return () => window.clearTimeout(timer)
  }, [active, delay])
  return on
}

/** Render children only after a short delay (use around skeletons). */
export function Delayed({ children, delay = SHOW_AFTER_MS }: { children: ReactNode; delay?: number }): ReactElement | null {
  const show = useDelayedFlag(true, delay)
  return show ? <div className="rj-fade-in">{children}</div> : null
}

export function SkeletonLine({ className = '' }: { className?: string }): ReactElement {
  return <div className={`skeleton h-3 ${className}`} aria-hidden="true" />
}

export function SkeletonBlock({ className = '' }: { className?: string }): ReactElement {
  return <div className={`skeleton ${className}`} aria-hidden="true" />
}

export function GlassTile({ children, className = '' }: { children: ReactNode; className?: string }): ReactElement {
  return <div className={`glass rounded-xl p-5 ${className}`}>{children}</div>
}

/** Wraps a skeleton so assistive tech announces a busy region once. */
export function LoadingRegion({ label = 'Loading', children }: { label?: string; children: ReactNode }): ReactElement {
  return (
    <div role="status" aria-busy="true" aria-live="polite">
      <span className="sr-only">{label}…</span>
      {children}
    </div>
  )
}

/** The Rejecta dot, pulsing. */
export function PulseDot({ className = '' }: { className?: string }): ReactElement {
  return <span className={`rj-dot inline-block h-2 w-2 rounded-full bg-red-600 ${className}`} aria-hidden="true" />
}

/**
 * Full-area loader for session restore and code-split chunks: a spinning red
 * ring around the Rejecta dot, a label, and a moving bar, over soft blurred
 * colour. Appears only after a short delay so fast loads never flash.
 */
export function PageLoader({ label = 'Loading' }: { label?: string }): ReactElement {
  const show = useDelayedFlag(true)
  return (
    <div className="relative flex min-h-[60vh] w-full items-center justify-center overflow-hidden px-4">
      {show ? (
        <>
          <span className="rj-blob -left-16 top-4 h-72 w-72 bg-gray-300" aria-hidden="true" />
          <span className="rj-blob -right-10 bottom-0 h-80 w-80 bg-red-200" aria-hidden="true" />
          <div className="rj-fade-in relative flex flex-col items-center" role="status" aria-busy="true" aria-live="polite">
            <div className="rj-orbit">
              <div className="glass absolute inset-[7px] flex items-center justify-center rounded-full">
                <PulseDot className="h-3 w-3" />
              </div>
            </div>
            <p className="mt-6 text-sm font-semibold tracking-tight text-gray-900">Rejecta</p>
            <p className="mt-1 text-xs text-gray-500">{label}…</p>
            <div className="mt-5 h-1 w-44 overflow-hidden rounded-full bg-gray-200">
              <span className="rj-bar" />
            </div>
          </div>
        </>
      ) : null}
    </div>
  )
}

/**
 * Keeps content on screen while it refreshes: blurred and non-clickable, with
 * a small glass pill. Prevents a double click on something already saving.
 */
export function RefreshOverlay({
  active,
  label = 'Updating',
  className = '',
  children,
}: {
  active: boolean
  label?: string
  /** Classes for the box around the children (for example h-full). */
  className?: string
  children: ReactNode
}): ReactElement {
  const show = useDelayedFlag(active)
  return (
    <div className="relative h-full" aria-busy={show}>
      <div
        className={`transition duration-200 ${className} ${show ? 'pointer-events-none select-none opacity-60 blur-[3px]' : ''}`}
        aria-hidden={show}
      >
        {children}
      </div>
      {show ? (
        <div className="pointer-events-none absolute inset-x-0 top-6 z-10 flex justify-center" role="status">
          <span className="glass rj-fade-in inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium text-gray-700">
            <PulseDot />
            {label}…
          </span>
        </div>
      ) : null}
    </div>
  )
}

/**
 * For model calls that take 15-45 s: a glass tile whose status line changes
 * every few seconds, so the wait feels alive and people can tell it is working.
 */
export function WorkingTile({
  title = 'Working on it',
  steps,
  intervalMs = 4000,
  note = 'This can take up to a minute. Please keep this page open.',
}: {
  title?: string
  steps: string[]
  intervalMs?: number
  /** Footer line; pass an empty string to hide it. */
  note?: string
}): ReactElement {
  const [index, setIndex] = useState(0)
  useEffect(() => {
    if (steps.length < 2) return
    const timer = window.setInterval(() => setIndex((i) => Math.min(i + 1, steps.length - 1)), intervalMs)
    return () => window.clearInterval(timer)
  }, [steps.length, intervalMs])

  return (
    <div className="relative overflow-hidden rounded-xl" role="status" aria-busy="true" aria-live="polite">
      <span className="rj-blob -left-8 -top-6 h-40 w-40 bg-gray-200" aria-hidden="true" />
      <span className="rj-blob -bottom-8 right-0 h-44 w-44 bg-red-100" aria-hidden="true" />
      <GlassTile className="relative p-6">
        <div className="flex items-center gap-2 text-sm font-semibold tracking-tight text-gray-900">
          <PulseDot />
          {title}
        </div>
        <p key={index} className="rj-fade-in mt-3 text-sm text-gray-600">
          {steps[index] ?? 'Working'}…
        </p>
        <div className="mt-5 space-y-2.5">
          <SkeletonLine className="w-full" />
          <SkeletonLine className="w-11/12" />
          <SkeletonLine className="w-4/5" />
          <SkeletonLine className="w-3/5" />
        </div>
        {note ? <p className="mt-4 text-xs text-gray-500">{note}</p> : null}
      </GlassTile>
    </div>
  )
}

/**
 * Result area of a tool page. First run: a glass working tile. Re-run: the
 * previous result stays, blurred, so the page never goes blank or jumps.
 */
export function ToolWait({
  loading,
  hasResult,
  title,
  steps,
  note,
  className = '',
  children,
}: {
  loading: boolean
  hasResult: boolean
  title: string
  steps: string[]
  note?: string
  className?: string
  children: ReactNode
}): ReactElement {
  if (loading && !hasResult) {
    return (
      <Delayed>
        <WorkingTile title={title} steps={steps} note={note} />
      </Delayed>
    )
  }
  return (
    <RefreshOverlay active={loading} label={title} className={className}>
      {children}
    </RefreshOverlay>
  )
}
