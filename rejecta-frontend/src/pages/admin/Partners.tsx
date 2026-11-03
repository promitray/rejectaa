import type { ReactElement } from 'react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { adminApi } from '../../admin/api'
import type { NewPartnerSecret } from '../../admin/api'
import { attempt, useLoad } from '../../admin/useLoad'
import { API_BASE } from '../../auth/client'
import { ListSkeleton } from '../../components/AdminSkeletons'
import { Alert, Badge, Button, Card, CopyField, Field, Input, Modal, PageHeader, Pagination, Select, formatDate } from '../../components/ui'

export function SecretReveal({ credentials }: { credentials: NewPartnerSecret }): ReactElement {
  return (
    <div>
      <Alert tone="warning">Copy the secret now. It is shown only once and cannot be recovered.</Alert>
      <Field label="Client ID" htmlFor="c-id"><CopyField value={credentials.client_id} label="Client ID" /></Field>
      <Field label="Client secret" htmlFor="c-secret"><CopyField value={credentials.client_secret} label="Client secret" /></Field>
      <Field label="Token request">
        <pre className="overflow-x-auto rounded-lg bg-gray-900 p-3 text-xs text-gray-100">{`curl -X POST ${API_BASE}/oauth/token \\
  -d grant_type=client_credentials \\
  -d client_id=${credentials.client_id} \\
  -d client_secret=<secret>`}</pre>
      </Field>
    </div>
  )
}

export default function Partners(): ReactElement {
  const [search, setSearch] = useState('')
  const [applied, setApplied] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const partners = useLoad(
    () => adminApi.partners({ search: applied, status: statusFilter, page, page_size: pageSize }),
    [applied, statusFilter, page, pageSize],
  )
  const plans = useLoad(() => adminApi.plans())
  const features = useLoad(() => adminApi.features())
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ name: '', description: '', plan_id: '', scopes: [] as string[] })
  const [created, setCreated] = useState<NewPartnerSecret | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const partnerPlan = (plans.data ?? []).find((p) => p.key === 'partner')

  function start(): void {
    setCreated(null)
    setError(null)
    setForm({ name: '', description: '', plan_id: partnerPlan?.id ?? '', scopes: (features.data ?? []).map((f) => f.key) })
    setOpen(true)
  }

  async function create(): Promise<void> {
    setBusy(true)
    setError(null)
    await attempt(async () => {
      const result = await adminApi.createPartner(form)
      setCreated(result.credentials)
      void partners.reload()
    }, setError)
    setBusy(false)
  }

  return (
    <div>
      <PageHeader
        title="Partners"
        subtitle="Platforms such as ChemLabFlow that call Rejecta from their own servers (OAuth2 client credentials)."
        actions={<Button onClick={start}>New partner</Button>}
      />
      <Card className="mb-4">
        <form
          onSubmit={(e) => { e.preventDefault(); setPage(1); setApplied(search.trim()) }}
          className="grid gap-3 sm:grid-cols-[1fr_auto_auto]"
        >
          <Input placeholder="Search name or client ID" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search" />
          <Select value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1) }} aria-label="Status">
            <option value="">Any status</option>
            <option value="active">Active</option>
            <option value="disabled">Disabled</option>
          </Select>
          <Button type="submit" variant="secondary">Search</Button>
        </form>
      </Card>
      <Alert>{partners.error}</Alert>
      {partners.loading && !partners.data ? <ListSkeleton count={3} label="Loading partners" /> : null}
      <div className="flex flex-col gap-3">
        {(partners.data?.items ?? []).map((p) => (
          <Link key={p.id} to={`/admin/partners/${p.id}`}>
            <Card className="transition hover:border-gray-400">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <p className="flex items-center gap-2 font-medium text-gray-900">
                    {p.name} <Badge tone={p.status === 'active' ? 'green' : 'red'}>{p.status}</Badge>
                  </p>
                  <p className="font-mono text-xs text-gray-500">{p.client_id}</p>
                </div>
                <p className="text-xs text-gray-500">
                  {p.plan.name} plan · {p.scopes.length} scopes · last used {formatDate(p.last_used_at)}
                </p>
              </div>
            </Card>
          </Link>
        ))}
        {partners.data && partners.data.items.length === 0 ? (
          <Card><p className="text-sm text-gray-500">{applied || statusFilter ? 'No partners match.' : 'No partners yet.'}</p></Card>
        ) : null}
      </div>
      {partners.data ? (
        <Pagination
          page={page}
          pageSize={pageSize}
          total={partners.data.total}
          busy={partners.loading}
          onPage={setPage}
          onPageSize={(size) => { setPageSize(size); setPage(1) }}
        />
      ) : null}

      <Modal open={open} title={created ? 'Partner created' : 'New partner'} onClose={() => setOpen(false)}
        footer={created ? <Button onClick={() => setOpen(false)}>I saved the secret</Button> : (
          <><Button variant="secondary" onClick={() => setOpen(false)}>Cancel</Button>
            <Button loading={busy} disabled={!form.name || !form.plan_id || form.scopes.length === 0} onClick={() => void create()}>Create</Button></>
        )}>
        {created ? <SecretReveal credentials={created} /> : (
          <div>
            <Field label="Name" htmlFor="pn-name"><Input id="pn-name" value={form.name} maxLength={80} placeholder="ChemLabFlow" onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
            <Field label="Description" htmlFor="pn-desc"><Input id="pn-desc" value={form.description} maxLength={500} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Field>
            <Field label="Plan" htmlFor="pn-plan" hint="Limits for the whole partner (one shared pool).">
              <Select id="pn-plan" value={form.plan_id} onChange={(e) => setForm({ ...form, plan_id: e.target.value })}>
                <option value="">Choose a plan</option>
                {(plans.data ?? []).filter((p) => p.status === 'active').map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </Select>
            </Field>
            <ScopePicker features={features.data ?? []} value={form.scopes} onChange={(scopes) => setForm({ ...form, scopes })} />
            <div className="mt-4"><Alert>{error}</Alert></div>
          </div>
        )}
      </Modal>
    </div>
  )
}

export function ScopePicker({ features, value, onChange }: {
  features: { key: string; name: string }[]
  value: string[]
  onChange: (scopes: string[]) => void
}): ReactElement {
  return (
    <Field label="Allowed tools (scopes)">
      <div className="grid gap-1.5 sm:grid-cols-2">
        {features.map((f) => (
          <label key={f.key} className="flex items-center gap-2 text-sm text-gray-700">
            <input
              type="checkbox"
              checked={value.includes(f.key)}
              onChange={(e) => onChange(e.target.checked ? [...value, f.key] : value.filter((k) => k !== f.key))}
            />
            {f.name}
          </label>
        ))}
      </div>
    </Field>
  )
}
