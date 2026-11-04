import type { ReactElement } from 'react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { adminApi } from '../../admin/api'
import type { AdminPlan, PlanFeature } from '../../admin/api'
import { attempt, useLoad } from '../../admin/useLoad'
import { ApiError } from '../../auth/client'
import { PlanEditorSkeleton } from '../../components/AdminSkeletons'
import { Alert, Badge, Button, Card, Field, Input, Modal, PageHeader, Select, Toggle } from '../../components/ui'
import LimitInput from './LimitInput'
import { slugify } from './Plans'

type Row = Pick<PlanFeature, 'feature_key' | 'name' | 'globally_enabled' | 'enabled' | 'daily_limit' | 'monthly_limit'>

function rowError(r: Row): string | null {
  if (r.daily_limit !== null && r.monthly_limit !== null && r.daily_limit > r.monthly_limit) {
    return 'Daily limit is higher than the monthly limit.'
  }
  return null
}

export default function PlanEditor(): ReactElement {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const plan = useLoad(() => adminApi.plan(id), [id])
  const allPlans = useLoad(() => adminApi.plans())
  const [period, setPeriod] = useState<'day' | 'month'>('month')
  const usage = useLoad(() => adminApi.planUsage(id, period), [id, period])

  const [rows, setRows] = useState<Row[]>([])
  const [details, setDetails] = useState({ name: '', description: '', sort_order: 0 })
  const [message, setMessage] = useState<{ tone: 'error' | 'success' | 'warning'; text: string } | null>(null)
  const [conflict, setConflict] = useState(false)
  const [busy, setBusy] = useState(false)
  const [dupOpen, setDupOpen] = useState(false)
  const [dup, setDup] = useState({ name: '', key: '' })
  const [migrateOpen, setMigrateOpen] = useState(false)
  const [migrateTo, setMigrateTo] = useState('')

  const p: AdminPlan | null = plan.data
  useEffect(() => {
    if (!p) return
    setRows(p.features.map((f) => ({ ...f })))
    setDetails({ name: p.name, description: p.description, sort_order: p.sort_order })
  }, [p])

  const dirtyMatrix = useMemo(
    () => !!p && JSON.stringify(rows) !== JSON.stringify(p.features),
    [rows, p],
  )
  const dirtyDetails = !!p && (details.name !== p.name || details.description !== p.description || details.sort_order !== p.sort_order)
  const invalid = rows.some((r) => rowError(r) !== null)

  useEffect(() => {
    if (!dirtyMatrix && !dirtyDetails) return
    const warn = (event: BeforeUnloadEvent): void => event.preventDefault()
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirtyMatrix, dirtyDetails])

  if (!p) return plan.error ? <Alert>{plan.error}</Alert> : <PlanEditorSkeleton />

  function onError(text: string, err?: unknown): void {
    if (err instanceof ApiError && err.code === 'version_conflict') setConflict(true)
    setMessage({ tone: 'error', text })
  }

  async function run(action: () => Promise<AdminPlan | unknown>, success: string): Promise<void> {
    setBusy(true)
    setMessage(null)
    try {
      const result = await action()
      if (result && typeof result === 'object' && 'features' in result) plan.setData(result as AdminPlan)
      else await plan.reload()
      setMessage({ tone: 'success', text: success })
      void usage.reload()
      void allPlans.reload()
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Action failed.', err)
    } finally {
      setBusy(false)
    }
  }

  const update = (index: number, patch: Partial<Row>): void =>
    setRows(rows.map((r, i) => (i === index ? { ...r, ...patch } : r)))

  const saveMatrix = (): Promise<void> =>
    run(
      () => adminApi.savePlanFeatures(p.id, p.version, rows.map(({ feature_key, enabled, daily_limit, monthly_limit }) => ({
        feature_key, enabled, daily_limit, monthly_limit,
      }))),
      'Features and limits saved. They apply from the next request.',
    )

  const saveDetails = (): Promise<void> =>
    run(() => adminApi.updatePlan(p.id, { version: p.version, ...details }), 'Plan details saved.')

  const otherActive = (allPlans.data ?? []).filter((x) => x.id !== p.id && x.status === 'active')

  return (
    <div>
      <p className="mb-2 text-xs text-gray-500"><Link to="/admin/plans" className="hover:underline">Plans</Link> /</p>
      <PageHeader
        title={p.name}
        subtitle={<>
          <span className="font-mono text-xs">{p.key}</span>{' '}
          {p.is_default ? <Badge tone="black">default for new sign-ups</Badge> : null}{' '}
          {p.status === 'archived' ? <Badge>archived</Badge> : null}{' '}
          <span className="text-xs">{p.user_count} users · {p.client_count} partners</span>
        </>}
        actions={<>
          {!p.is_default && p.status === 'active' ? (
            <Button variant="secondary" disabled={busy}
              onClick={() => void run(() => adminApi.updatePlan(p.id, { version: p.version, is_default: true }), `${p.name} is now the default plan.`)}>
              Make default
            </Button>
          ) : null}
          <Button variant="secondary" disabled={busy} onClick={() => { setDup({ name: `${p.name} copy`, key: `${p.key}-copy` }); setDupOpen(true) }}>Duplicate</Button>
          {p.user_count + p.client_count > 0 ? (
            <Button variant="secondary" disabled={busy} onClick={() => { setMigrateTo(''); setMigrateOpen(true) }}>Move users…</Button>
          ) : null}
          {p.status === 'active' ? (
            <Button variant="danger" disabled={busy || p.is_default}
              title={p.is_default ? 'Make another plan the default first' : undefined}
              onClick={() => {
                if (window.confirm(`Archive ${p.name}? It can be restored later.`))
                  void run(() => adminApi.updatePlan(p.id, { version: p.version, status: 'archived' }), 'Plan archived.')
              }}>
              Archive
            </Button>
          ) : (
            <Button variant="secondary" disabled={busy}
              onClick={() => void run(() => adminApi.updatePlan(p.id, { version: p.version, status: 'active' }), 'Plan restored.')}>
              Restore
            </Button>
          )}
        </>}
      />

      {conflict ? (
        <div className="mb-4">
          <Alert tone="warning">
            Another admin changed this plan while you were editing.{' '}
            <button type="button" className="font-medium underline" onClick={() => { setConflict(false); setMessage(null); void plan.reload() }}>
              Reload the latest version
            </button>{' '}
            (your unsaved edits will be replaced).
          </Alert>
        </div>
      ) : message ? <div className="mb-4"><Alert tone={message.tone}>{message.text}</Alert></div> : null}

      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold text-gray-900">Features & limits</h2>
            <p className="mt-1 text-xs text-gray-500">Daily limits reset at 00:00 UTC, monthly on the 1st. Per-user overrides take precedence.</p>
          </div>
          <div className="flex gap-2">
            <Button variant="secondary" disabled={!dirtyMatrix || busy} onClick={() => setRows(p.features.map((f) => ({ ...f })))}>Discard</Button>
            <Button disabled={!dirtyMatrix || invalid} loading={busy} onClick={() => void saveMatrix()}>Save changes</Button>
          </div>
        </div>
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-gray-500">
              <tr>
                <th className="pb-2 font-medium">Feature</th>
                <th className="pb-2 font-medium">Included</th>
                <th className="pb-2 font-medium">Daily limit</th>
                <th className="pb-2 font-medium">Monthly limit</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {rows.map((r, i) => {
                const problem = rowError(r)
                return (
                  <tr key={r.feature_key} className={r.enabled ? '' : 'text-gray-400'}>
                    <td className="py-3 pr-3">
                      <p className={`font-medium ${r.enabled ? 'text-gray-900' : 'text-gray-500'}`}>{r.name}</p>
                      {!r.globally_enabled ? <p className="text-xs text-red-600">Switched off for everyone (Features page)</p> : null}
                      {problem ? <p className="text-xs text-red-600">{problem}</p> : null}
                    </td>
                    <td className="py-3 pr-3"><Toggle checked={r.enabled} label={`${r.name} included`} onChange={(v) => update(i, { enabled: v })} /></td>
                    <td className="py-3 pr-3"><LimitInput value={r.daily_limit} disabled={!r.enabled} label={`${r.name} daily limit`} onChange={(v) => update(i, { daily_limit: v })} /></td>
                    <td className="py-3"><LimitInput value={r.monthly_limit} disabled={!r.enabled} label={`${r.name} monthly limit`} onChange={(v) => update(i, { monthly_limit: v })} /></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card>
          <h2 className="text-sm font-semibold text-gray-900">Details</h2>
          <Field label="Name" htmlFor="pe-name">
            <Input id="pe-name" value={details.name} maxLength={80} onChange={(e) => setDetails({ ...details, name: e.target.value })} />
          </Field>
          <Field label="Description" htmlFor="pe-desc">
            <Input id="pe-desc" value={details.description} maxLength={500} onChange={(e) => setDetails({ ...details, description: e.target.value })} />
          </Field>
          <Field label="Sort order" htmlFor="pe-sort" hint="Lower numbers are listed first.">
            <Input id="pe-sort" type="number" value={details.sort_order} onChange={(e) => setDetails({ ...details, sort_order: Number(e.target.value) || 0 })} />
          </Field>
          <Button className="mt-4" disabled={!dirtyDetails || !details.name.trim()} loading={busy} onClick={() => void saveDetails()}>Save details</Button>
        </Card>

        <Card>
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-900">Usage on this plan</h2>
            <Select value={period} onChange={(e) => setPeriod(e.target.value as 'day' | 'month')} className="w-auto" aria-label="Period">
              <option value="day">Today</option>
              <option value="month">This month</option>
            </Select>
          </div>
          <ul className="mt-3 divide-y divide-gray-100 text-sm">
            {(usage.data?.rows ?? []).map((r) => (
              <li key={r.feature_key} className="flex justify-between py-2">
                <span className="text-gray-800">{r.name}</span>
                <span className="text-gray-600">{r.used} runs · {r.subjects} {r.subjects === 1 ? 'account' : 'accounts'}</span>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <Modal open={dupOpen} title="Duplicate plan" onClose={() => setDupOpen(false)}
        footer={<><Button variant="secondary" onClick={() => setDupOpen(false)}>Cancel</Button>
          <Button loading={busy} disabled={!dup.name || !dup.key} onClick={() => void attempt(async () => {
            const copy = await adminApi.duplicatePlan(p.id, dup)
            setDupOpen(false)
            navigate(`/admin/plans/${copy.id}`)
          }, (t) => setMessage({ tone: 'error', text: t }))}>Duplicate</Button></>}>
        <Field label="Name" htmlFor="d-name"><Input id="d-name" value={dup.name} onChange={(e) => setDup({ name: e.target.value, key: slugify(e.target.value) })} /></Field>
        <Field label="Key" htmlFor="d-key"><Input id="d-key" className="font-mono" value={dup.key} onChange={(e) => setDup({ ...dup, key: e.target.value.toLowerCase() })} /></Field>
      </Modal>

      <Modal open={migrateOpen} title="Move users to another plan" onClose={() => setMigrateOpen(false)}
        footer={<><Button variant="secondary" onClick={() => setMigrateOpen(false)}>Cancel</Button>
          <Button loading={busy} disabled={!migrateTo} onClick={() => void run(async () => {
            const result = await adminApi.migratePlan(p.id, migrateTo)
            setMigrateOpen(false)
            return result
          }, 'Users moved.')}>Move everyone</Button></>}>
        <p className="text-sm text-gray-600">All {p.user_count} users and {p.client_count} partners on {p.name} will move. Their current usage counts carry over.</p>
        <Field label="Target plan" htmlFor="m-to">
          <Select id="m-to" value={migrateTo} onChange={(e) => setMigrateTo(e.target.value)}>
            <option value="">Choose a plan</option>
            {otherActive.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </Select>
        </Field>
      </Modal>
    </div>
  )
}
