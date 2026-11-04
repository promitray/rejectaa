/**
 * Small UI kit in the app's existing style: white cards with gray-200
 * borders, black primary buttons, rounded-lg inputs, text-sm, red-600 accents.
 */
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactElement, ReactNode, SelectHTMLAttributes } from 'react'
import { useEffect, useState } from 'react'

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost'

const VARIANTS: Record<Variant, string> = {
  primary: 'bg-black text-white hover:bg-gray-800',
  secondary: 'border border-gray-300 bg-white text-gray-800 hover:border-gray-500',
  danger: 'border border-red-200 bg-white text-red-700 hover:border-red-400 hover:bg-red-50',
  ghost: 'text-gray-600 hover:text-gray-900',
}

export function Button({
  variant = 'primary',
  loading = false,
  className = '',
  children,
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; loading?: boolean }): ReactElement {
  return (
    <button
      type="button"
      disabled={disabled || loading}
      className={`inline-flex items-center justify-center gap-2 rounded-lg px-3.5 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-60 ${VARIANTS[variant]} ${className}`}
      {...rest}
    >
      {loading ? <Spinner light={variant === 'primary'} /> : null}
      {children}
    </button>
  )
}

export function Spinner({ light = false }: { light?: boolean }): ReactElement {
  return (
    <span
      className={`h-4 w-4 animate-spin rounded-full border-2 ${light ? 'border-white/30 border-t-white' : 'border-gray-300 border-t-gray-800'}`}
      aria-hidden="true"
    />
  )
}

export function Field({
  label,
  hint,
  error,
  children,
  htmlFor,
}: {
  label: string
  hint?: ReactNode
  error?: string | null
  children: ReactNode
  htmlFor?: string
}): ReactElement {
  return (
    <div className="mt-4 first:mt-0">
      <label htmlFor={htmlFor} className="block text-sm font-medium text-gray-800">
        {label}
      </label>
      <div className="mt-1">{children}</div>
      {hint && !error ? <p className="mt-1 text-xs text-gray-500">{hint}</p> : null}
      {error ? <p className="mt-1 text-sm text-red-600">{error}</p> : null}
    </div>
  )
}

const INPUT = 'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm outline-none focus:border-gray-900 disabled:bg-gray-50'

export function Input({ className = '', ...rest }: InputHTMLAttributes<HTMLInputElement>): ReactElement {
  return <input className={`${INPUT} ${className}`} {...rest} />
}

/** Password field with a show/hide (eye) button. */
export function PasswordInput({
  className = '',
  ...rest
}: Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>): ReactElement {
  const [visible, setVisible] = useState(false)
  return (
    <div className="relative">
      <input type={visible ? 'text' : 'password'} className={`${INPUT} pr-10 ${className}`} {...rest} />
      <button
        type="button"
        onClick={() => setVisible(!visible)}
        aria-label={visible ? 'Hide password' : 'Show password'}
        aria-pressed={visible}
        title={visible ? 'Hide password' : 'Show password'}
        className="absolute inset-y-0 right-0 flex w-10 items-center justify-center text-gray-500 hover:text-gray-900"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z" />
          <circle cx="12" cy="12" r="3" />
          {visible ? <path d="M4 4l16 16" /> : null}
        </svg>
      </button>
    </div>
  )
}

export function Select({ className = '', children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>): ReactElement {
  return (
    <select className={`${INPUT} ${className}`} {...rest}>
      {children}
    </select>
  )
}

export function Card({ children, className = '' }: { children: ReactNode; className?: string }): ReactElement {
  return <div className={`rounded-xl border border-gray-200 bg-white p-5 shadow-sm ${className}`}>{children}</div>
}

export function Alert({
  tone = 'error',
  children,
}: {
  tone?: 'error' | 'info' | 'success' | 'warning'
  children: ReactNode
}): ReactElement | null {
  if (!children) return null
  const tones = {
    error: 'border-red-200 bg-red-50 text-red-800',
    info: 'border-gray-200 bg-gray-50 text-gray-700',
    success: 'border-green-200 bg-green-50 text-green-800',
    warning: 'border-amber-200 bg-amber-50 text-amber-900',
  }
  return <div className={`rounded-lg border px-3 py-2 text-sm ${tones[tone]}`} role={tone === 'error' ? 'alert' : 'status'}>{children}</div>
}

export function Badge({
  tone = 'gray',
  children,
}: {
  tone?: 'gray' | 'green' | 'red' | 'amber' | 'black'
  children: ReactNode
}): ReactElement {
  const tones = {
    gray: 'bg-gray-100 text-gray-700',
    green: 'bg-green-50 text-green-700 ring-1 ring-green-200',
    red: 'bg-red-50 text-red-700 ring-1 ring-red-200',
    amber: 'bg-amber-50 text-amber-800 ring-1 ring-amber-200',
    black: 'bg-gray-900 text-white',
  }
  return <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${tones[tone]}`}>{children}</span>
}

export function Toggle({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean
  onChange: (next: boolean) => void
  label: string
  disabled?: boolean
}): ReactElement {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition disabled:opacity-50 ${checked ? 'bg-gray-900' : 'bg-gray-300'}`}
    >
      <span className={`inline-block h-4 w-4 rounded-full bg-white shadow transition ${checked ? 'translate-x-4' : 'translate-x-0.5'}`} />
    </button>
  )
}

export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
}): ReactElement | null {
  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 px-4" role="dialog" aria-modal="true" aria-label={title}>
      <div className="w-full max-w-lg rounded-xl border border-gray-200 bg-white shadow-lg">
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3">
          <h2 className="text-base font-semibold tracking-tight">{title}</h2>
          <button type="button" onClick={onClose} className="text-gray-500 hover:text-gray-900" aria-label="Close">
            ✕
          </button>
        </div>
        <div className="max-h-[70vh] overflow-y-auto px-5 py-4">{children}</div>
        {footer ? <div className="flex justify-end gap-2 border-t border-gray-200 px-5 py-3">{footer}</div> : null}
      </div>
    </div>
  )
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string
  subtitle?: ReactNode
  actions?: ReactNode
}): ReactElement {
  return (
    <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-gray-900">{title}</h1>
        {subtitle ? <p className="mt-1 text-sm text-gray-600">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
    </div>
  )
}

export function CopyField({ value, label }: { value: string; label: string }): ReactElement {
  return (
    <div className="flex gap-2">
      <Input readOnly value={value} aria-label={label} className="font-mono text-xs" onFocus={(e) => e.currentTarget.select()} />
      <Button variant="secondary" onClick={() => void navigator.clipboard.writeText(value)}>
        Copy
      </Button>
    </div>
  )
}

export function UsageBar({ used, limit }: { used: number; limit: number | null }): ReactElement {
  if (limit === null) {
    return <span className="text-xs text-gray-500">{used} used · unlimited</span>
  }
  const pct = limit === 0 ? 100 : Math.min(100, Math.round((used / limit) * 100))
  const color = pct >= 100 ? 'bg-red-600' : pct >= 80 ? 'bg-amber-500' : 'bg-gray-900'
  return (
    <div className="min-w-[120px]">
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-100">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <p className="mt-1 text-xs text-gray-500">
        {used} / {limit}
      </p>
    </div>
  )
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  return new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function limitLabel(limit: number | null): string {
  return limit === null ? 'Unlimited' : String(limit)
}

type PageItem = number | 'gap-left' | 'gap-right'

/** 1 … 4 5 6 … 20, never more than 7 slots. */
export function pageWindow(page: number, pages: number): PageItem[] {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1)
  const items: PageItem[] = [1]
  const start = Math.max(2, page - 1)
  const end = Math.min(pages - 1, page + 1)
  if (start > 2) items.push('gap-left')
  for (let p = start; p <= end; p++) items.push(p)
  if (end < pages - 1) items.push('gap-right')
  items.push(pages)
  return items
}

const PAGE_BUTTON =
  'inline-flex h-8 min-w-[2rem] items-center justify-center rounded-lg border px-2 text-sm transition disabled:cursor-not-allowed disabled:opacity-40'

/** Server-side pagination controls: range summary, page numbers, per-page size. */
export function Pagination({
  page,
  pageSize,
  total,
  onPage,
  onPageSize,
  pageSizes = [10, 25, 50, 100],
  busy = false,
}: {
  page: number
  pageSize: number
  total: number
  onPage: (page: number) => void
  onPageSize?: (size: number) => void
  pageSizes?: number[]
  busy?: boolean
}): ReactElement {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1
  const to = Math.min(total, page * pageSize)

  // Filters can shrink the list below the current page: step back to the last page.
  useEffect(() => {
    if (!busy && total > 0 && page > pages) onPage(pages)
  }, [busy, total, page, pages, onPage])

  const go = (target: number): void => onPage(Math.min(pages, Math.max(1, target)))

  return (
    <nav aria-label="Pagination" className="mt-3 flex flex-wrap items-center justify-between gap-3 text-sm">
      <p className="text-gray-600" aria-live="polite">
        {total === 0 ? 'No results' : `Showing ${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()}`}
      </p>
      <div className="flex flex-wrap items-center gap-3">
        {onPageSize ? (
          <label className="flex items-center gap-2 text-gray-600">
            <span className="text-xs">Per page</span>
            <select
              value={pageSize}
              disabled={busy}
              onChange={(e) => onPageSize(Number(e.target.value))}
              className="rounded-lg border border-gray-300 bg-white px-2 py-1 text-sm outline-none focus:border-gray-900"
            >
              {pageSizes.map((size) => (
                <option key={size} value={size}>{size}</option>
              ))}
            </select>
          </label>
        ) : null}
        {pages > 1 ? (
          <div className="flex items-center gap-1">
            <button type="button" className={`${PAGE_BUTTON} border-gray-300 bg-white text-gray-700 hover:border-gray-500`} disabled={busy || page <= 1} onClick={() => go(1)} aria-label="First page">«</button>
            <button type="button" className={`${PAGE_BUTTON} border-gray-300 bg-white text-gray-700 hover:border-gray-500`} disabled={busy || page <= 1} onClick={() => go(page - 1)} aria-label="Previous page">‹</button>
            {pageWindow(page, pages).map((item) =>
              typeof item === 'number' ? (
                <button
                  key={item}
                  type="button"
                  disabled={busy}
                  aria-current={item === page ? 'page' : undefined}
                  aria-label={`Page ${item}`}
                  onClick={() => go(item)}
                  className={`${PAGE_BUTTON} ${item === page ? 'border-gray-900 bg-gray-900 text-white' : 'border-gray-300 bg-white text-gray-700 hover:border-gray-500'}`}
                >
                  {item}
                </button>
              ) : (
                <span key={item} className="px-1 text-gray-400" aria-hidden="true">…</span>
              ),
            )}
            <button type="button" className={`${PAGE_BUTTON} border-gray-300 bg-white text-gray-700 hover:border-gray-500`} disabled={busy || page >= pages} onClick={() => go(page + 1)} aria-label="Next page">›</button>
            <button type="button" className={`${PAGE_BUTTON} border-gray-300 bg-white text-gray-700 hover:border-gray-500`} disabled={busy || page >= pages} onClick={() => go(pages)} aria-label="Last page">»</button>
          </div>
        ) : null}
      </div>
    </nav>
  )
}
