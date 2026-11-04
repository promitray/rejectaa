import type { ReactElement } from 'react'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { adminApi } from '../../admin/api'
import type { NewPartnerSecret } from '../../admin/api'
import { attempt, useLoad } from '../../admin/useLoad'
import { DetailSkeleton } from '../../components/AdminSkeletons'
import { Alert, Badge, Button, Card, Field, Input, Modal, PageHeader, Select, UsageBar, formatDate } from '../../components/ui'
import { ScopePicker, SecretReveal } from './Partners'

export default function PartnerDetail(): ReactElement {
  const { id = '' } = useParams()
  const partner = useLoad(() => adminApi.partner(id), [id])
  const plans = useLoad(() => adminApi.plans())
  const features = useLoad(() => adminApi.features())
  const [form, setForm] = useState({ name: '', description: '', plan_id: '', scopes: [] as string[] })
  const [message, setMessage] = useState<{ tone: 'error' | 'success'; text: string } | null>(null)
  const [secret, setSecret] = useState<NewPartnerSecret | null>(null)
  const [busy, setBusy] = useState(false)

  const p = partner.data
  useEffect(() => {
    if (p) setForm({ name: p.name, description: p.description, plan_id: p.plan.id, scopes: p.scopes })
  }, [p])

  if (!p) return partner.error ? <Alert>{partner.error}</Alert> : <DetailSkeleton label="Loading partner" />

  async function act(action: () => Promise<unknown>, success: string, confirmText?: string): Promise<void> {
    if (confirmText && !window.confirm(confirmText)) return
    setBusy(true)
    setMessage(null)
    if (await attempt(action, (t) => setMessage({ tone: 'error', text: t }))) {
      setMessage({ tone: 'success', text: success })
      await partner.reload()
    }
    setBusy(false)
  }

  const active = p.secrets.filter((s) => !s.revoked_at)

  return (
    <div>
      <p className="mb-2 text-xs text-gray-500"><Link to="/admin/partners" className="hover:underline">Partners</Link> /</p>
      <PageHeader
        title={p.name}
        subtitle={<><span className="font-mono text-xs">{p.client_id}</span>{' '}<Badge tone={p.status === 'active' ? 'green' : 'red'}>{p.status}</Badge></>}
        actions={p.status === 'active' ? (
          <Button variant="danger" disabled={busy} onClick={() => void act(() => adminApi.updatePartner(p.id, { status: 'disabled' }),
            'Partner disabled. Its tokens stopped working.', `Disable ${p.name}? All its API calls will fail immediately.`)}>Disable</Button>
        ) : (
          <Button variant="secondary" disabled={busy} onClick={() => void act(() => adminApi.updatePartner(p.id, { status: 'active' }), 'Partner enabled.')}>Enable</Button>
        )}
      />
      {message ? <div className="mb-4"><Alert tone={message.tone}>{message.text}</Alert></div> : null}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <h2 className="text-sm font-semibold text-gray-900">Settings</h2>
          <Field label="Name" htmlFor="pd-name"><Input id="pd-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label="Description" htmlFor="pd-desc"><Input id="pd-desc" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Field>
          <Field label="Plan" htmlFor="pd-plan">
            <Select id="pd-plan" value={form.plan_id} onChange={(e) => setForm({ ...form, plan_id: e.target.value })}>
              {(plans.data ?? []).filter((x) => x.status === 'active' || x.id === p.plan.id).map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
            </Select>
          </Field>
          <ScopePicker features={features.data ?? []} value={form.scopes} onChange={(scopes) => setForm({ ...form, scopes })} />
          <p className="mt-2 text-xs text-gray-500">Changing the plan or scopes ends the partner's current tokens; it simply requests new ones.</p>
          <Button className="mt-4" loading={busy} disabled={!form.name || form.scopes.length === 0}
            onClick={() => void act(() => adminApi.updatePartner(p.id, form), 'Partner saved.')}>Save</Button>
        </Card>

        <div className="flex flex-col gap-6">
          <Card>
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-gray-900">Secrets</h2>
              <Button variant="secondary" disabled={busy || active.length >= 2}
                title={active.length >= 2 ? 'Revoke a secret first (max 2 active)' : undefined}
                onClick={() => void attempt(async () => { setSecret(await adminApi.addSecret(p.id)); await partner.reload() },
                  (t) => setMessage({ tone: 'error', text: t }))}>
                New secret
              </Button>
            </div>
            <p className="mt-1 text-xs text-gray-500">Rotate with no downtime: create a new secret, deploy it on the partner side, then revoke the old one.</p>
            <ul className="mt-3 divide-y divide-gray-100 text-sm">
              {p.secrets.map((s) => (
                <li key={s.id} className="flex items-center justify-between gap-2 py-2">
                  <div>
                    <p className="font-mono text-xs">{s.prefix}…{s.last4}</p>
                    <p className="text-xs text-gray-500">created {formatDate(s.created_at)} · last used {formatDate(s.last_used_at)}</p>
                  </div>
                  {s.revoked_at ? <Badge>revoked</Badge> : (
                    <Button variant="danger" disabled={busy} onClick={() => void act(() => adminApi.revokeSecret(p.id, s.id),
                      'Secret revoked. Tokens issued so far were ended.', 'Revoke this secret? The partner must switch to another one.')}>Revoke</Button>
                  )}
                </li>
              ))}
            </ul>
          </Card>

          <Card>
            <h2 className="text-sm font-semibold text-gray-900">Usage</h2>
            <table className="mt-3 w-full text-sm">
              <tbody className="divide-y divide-gray-100">
                {p.features.filter((f) => p.scopes.includes(f.key)).map((f) => (
                  <tr key={f.key}>
                    <td className="py-2 pr-3 text-gray-800">{f.name}</td>
                    <td className="py-2 pr-3"><UsageBar used={f.used_today} limit={f.daily_limit} /></td>
                    <td className="py-2"><UsageBar used={f.used_this_month} limit={f.monthly_limit} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </div>
      </div>

      <Modal open={secret !== null} title="New secret" onClose={() => setSecret(null)} footer={<Button onClick={() => setSecret(null)}>I saved it</Button>}>
        {secret ? <SecretReveal credentials={secret} /> : null}
      </Modal>
    </div>
  )
}
