import type { FormEvent, ReactElement } from 'react'
import { useState } from 'react'
import { adminApi } from '../../admin/api'
import type { AuditEntry } from '../../admin/api'
import { useLoad } from '../../admin/useLoad'
import { TableSkeleton } from '../../components/AdminSkeletons'
import { RefreshOverlay } from '../../components/Loaders'
import { Alert, Badge, Button, Card, Input, PageHeader, Pagination, Select, formatDate } from '../../components/ui'

const PRESETS = [
  { value: '', label: 'All events' },
  { value: 'admin.', label: 'Admin actions' },
  { value: 'auth.login', label: 'Sign-ins' },
  { value: 'auth.login_failed', label: 'Failed sign-ins' },
  { value: 'auth.mfa', label: 'Two-factor' },
  { value: 'auth.refresh_reuse_detected', label: 'Token theft alerts' },
  { value: 'partner.', label: 'Partner tokens' },
]

function Change({ entry }: { entry: AuditEntry }): ReactElement | null {
  if (!entry.before && !entry.after) return null
  return (
    <details className="mt-1">
      <summary className="cursor-pointer text-xs text-gray-500">Details</summary>
      <pre className="mt-1 overflow-x-auto rounded bg-gray-50 p-2 text-xs text-gray-700">
        {JSON.stringify({ before: entry.before ?? undefined, after: entry.after ?? undefined }, null, 2)}
      </pre>
    </details>
  )
}

export default function AuditLog(): ReactElement {
  const [action, setAction] = useState('')
  const [target, setTarget] = useState('')
  const [applied, setApplied] = useState({ action: '', target: '' })
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const audit = useLoad(
    () => adminApi.audit({ action: applied.action, target_id: applied.target, page, page_size: pageSize }),
    [applied, page, pageSize],
  )

  function submit(event: FormEvent): void {
    event.preventDefault()
    setPage(1)
    setApplied({ action, target: target.trim() })
  }

  const items = audit.data?.items ?? []
  return (
    <div>
      <PageHeader title="Audit log" subtitle="Every sign-in, security event and admin change, newest first. Entries cannot be edited or deleted." />
      <Card className="mb-4">
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-[auto_1fr_auto]">
          <Select value={action} onChange={(e) => setAction(e.target.value)} aria-label="Event type">
            {PRESETS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </Select>
          <Input placeholder="Target id (user, plan or partner id)" value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Target id" />
          <Button type="submit" variant="secondary">Filter</Button>
        </form>
      </Card>
      <Alert>{audit.error}</Alert>
      {audit.loading && !audit.data ? <TableSkeleton rows={8} label="Loading audit log" /> : (
      <RefreshOverlay active={audit.loading} label="Loading events">
      <div className="overflow-x-auto rounded-xl border border-gray-200 bg-white">
        <table className="w-full text-sm">
          <thead className="border-b border-gray-200 bg-gray-50 text-left text-xs text-gray-500">
            <tr><th className="px-4 py-2 font-medium">When</th><th className="px-4 py-2 font-medium">Event</th><th className="px-4 py-2 font-medium">By</th><th className="px-4 py-2 font-medium">Target</th><th className="px-4 py-2 font-medium">IP</th></tr>
          </thead>
          <tbody className="divide-y divide-gray-100 align-top">
            {items.map((e) => (
              <tr key={e.id}>
                <td className="whitespace-nowrap px-4 py-2 text-gray-600">{formatDate(e.created_at)}</td>
                <td className="px-4 py-2">
                  <Badge tone={e.action.includes('reuse') || e.action.endsWith('failed') ? 'red' : e.action.startsWith('admin.') ? 'black' : 'gray'}>{e.action}</Badge>
                  <Change entry={e} />
                </td>
                <td className="px-4 py-2 text-gray-700">{e.actor_email ?? e.actor_type}</td>
                <td className="px-4 py-2 font-mono text-xs text-gray-500">{e.target_type ? `${e.target_type}:` : ''}{e.target_id?.slice(0, 8)}</td>
                <td className="px-4 py-2 text-xs text-gray-500">{e.ip ?? '—'}</td>
              </tr>
            ))}
            {audit.data && items.length === 0 ? <tr><td colSpan={5} className="px-4 py-6 text-center text-gray-500">No events.</td></tr> : null}
          </tbody>
        </table>
      </div>
      </RefreshOverlay>
      )}
      {audit.data ? (
        <Pagination
          page={page}
          pageSize={pageSize}
          total={audit.data.total}
          busy={audit.loading}
          onPage={setPage}
          onPageSize={(size) => { setPageSize(size); setPage(1) }}
        />
      ) : null}
    </div>
  )
}
