import type { ReactElement } from 'react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { adminApi } from '../../admin/api'
import { attempt, useLoad } from '../../admin/useLoad'
import { CardGridSkeleton } from '../../components/AdminSkeletons'
import { Alert, Badge, Button, Card, Field, Input, Modal, PageHeader, Select, limitLabel } from '../../components/ui'

export function slugify(value: string): string {
  return value.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48)
}

export default function Plans(): ReactElement {
  const navigate = useNavigate()
  const plans = useLoad(() => adminApi.plans())
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ name: '', key: '', description: '', copy: '' })
  const [keyTouched, setKeyTouched] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function create(): Promise<void> {
    setBusy(true)
    setError(null)
    await attempt(async () => {
      const plan = await adminApi.createPlan({
        key: form.key, name: form.name, description: form.description, copy_from_plan_id: form.copy || null,
      })
      navigate(`/admin/plans/${plan.id}`)
    }, setError)
    setBusy(false)
  }

  return (
    <div>
      <PageHeader
        title="Plans"
        subtitle="What each plan includes and how much of it. Changes apply to everyone on the plan immediately."
        actions={<Button onClick={() => { setOpen(true); setForm({ name: '', key: '', description: '', copy: '' }); setKeyTouched(false) }}>New plan</Button>}
      />
      <Alert>{plans.error}</Alert>
      {plans.loading && !plans.data ? <CardGridSkeleton count={6} label="Loading plans" /> : null}

      <div className="grid gap-4 md:grid-cols-2">
        {(plans.data ?? []).map((p) => (
          <Link key={p.id} to={`/admin/plans/${p.id}`} className="block">
            <Card className={`h-full transition hover:border-gray-400 ${p.status === 'archived' ? 'opacity-60' : ''}`}>
              <div className="flex items-start justify-between gap-2">
                <div>
                  <h2 className="text-base font-semibold tracking-tight text-gray-900">{p.name}</h2>
                  <p className="font-mono text-xs text-gray-500">{p.key}</p>
                </div>
                <div className="flex gap-1">
                  {p.is_default ? <Badge tone="black">default</Badge> : null}
                  {p.status === 'archived' ? <Badge>archived</Badge> : null}
                </div>
              </div>
              {p.description ? <p className="mt-2 text-sm text-gray-600">{p.description}</p> : null}
              <p className="mt-3 text-xs text-gray-500">
                {p.user_count} users{p.client_count ? ` · ${p.client_count} partners` : ''} · {p.usage_this_month} runs this month
              </p>
              <ul className="mt-3 space-y-1 text-xs">
                {p.features.map((f) => (
                  <li key={f.feature_key} className="flex justify-between gap-2">
                    <span className={f.enabled ? 'text-gray-800' : 'text-gray-400 line-through'}>{f.name}</span>
                    <span className="text-gray-500">
                      {f.enabled ? `${limitLabel(f.daily_limit)}/day · ${limitLabel(f.monthly_limit)}/month` : 'not included'}
                    </span>
                  </li>
                ))}
              </ul>
            </Card>
          </Link>
        ))}
      </div>

      <Modal
        open={open}
        title="New plan"
        onClose={() => setOpen(false)}
        footer={<><Button variant="secondary" onClick={() => setOpen(false)}>Cancel</Button><Button loading={busy} disabled={!form.name || !form.key} onClick={() => void create()}>Create plan</Button></>}
      >
        <Field label="Name" htmlFor="p-name">
          <Input id="p-name" value={form.name} maxLength={80}
            onChange={(e) => setForm({ ...form, name: e.target.value, key: keyTouched ? form.key : slugify(e.target.value) })} />
        </Field>
        <Field label="Key" htmlFor="p-key" hint="Lower-case letters, numbers, - and _. Cannot be changed later.">
          <Input id="p-key" value={form.key} className="font-mono"
            onChange={(e) => { setKeyTouched(true); setForm({ ...form, key: e.target.value.toLowerCase() }) }} />
        </Field>
        <Field label="Description" htmlFor="p-desc">
          <Input id="p-desc" value={form.description} maxLength={500} onChange={(e) => setForm({ ...form, description: e.target.value })} />
        </Field>
        <Field label="Start from" htmlFor="p-copy" hint="Copy features and limits from an existing plan, or start with everything off.">
          <Select id="p-copy" value={form.copy} onChange={(e) => setForm({ ...form, copy: e.target.value })}>
            <option value="">Everything off</option>
            {(plans.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
        </Field>
        <div className="mt-4"><Alert>{error}</Alert></div>
      </Modal>
    </div>
  )
}
