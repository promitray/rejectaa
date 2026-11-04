import type { FormEvent, ReactElement } from 'react'
import { useState } from 'react'
import { useAuth } from '../auth/AuthProvider'
import { apiJson } from '../auth/client'
import { Alert, Badge, Button, Card, Field, Modal, PasswordInput, PageHeader, UsageBar, formatDate } from '../components/ui'

export default function Account(): ReactElement {
  const { me, logoutAll, changePassword } = useAuth()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [message, setMessage] = useState<{ tone: 'error' | 'success'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [codes, setCodes] = useState<string[] | null>(null)

  if (!me) return <></>

  function submitPassword(event: FormEvent): void {
    event.preventDefault()
    setBusy(true)
    setMessage(null)
    changePassword(current, next)
      .then(() => {
        setCurrent('')
        setNext('')
        setMessage({ tone: 'success', text: 'Password changed. Your other devices were signed out.' })
      })
      .catch((err: unknown) => setMessage({ tone: 'error', text: err instanceof Error ? err.message : 'Could not change password.' }))
      .finally(() => setBusy(false))
  }

  return (
    <div className="mx-auto max-w-5xl px-4 py-10">
      <PageHeader
        title="Your account"
        subtitle={
          <>
            {me.email} · <Badge tone={me.role === 'admin' ? 'black' : 'gray'}>{me.role}</Badge>{' '}
            <Badge>{me.plan?.name ?? 'No plan'} plan</Badge>
          </>
        }
      />

      <div className="grid gap-6 md:grid-cols-[1.4fr_1fr]">
        <Card>
          <h2 className="text-sm font-semibold text-gray-900">Usage</h2>
          <p className="mt-1 text-xs text-gray-500">Daily limits reset at midnight UTC; monthly limits on the 1st.</p>
          <table className="mt-4 w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-500">
                <th className="pb-2 font-medium">Tool</th>
                <th className="pb-2 font-medium">Today</th>
                <th className="pb-2 font-medium">This month</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {me.features.map((f) => (
                <tr key={f.key}>
                  <td className="py-3 pr-3">
                    <p className="font-medium text-gray-900">{f.name}</p>
                    {!f.enabled ? (
                      <p className="text-xs text-gray-500">{f.reason === 'feature_disabled' ? 'Temporarily unavailable' : 'Not in your plan'}</p>
                    ) : null}
                  </td>
                  <td className="py-3 pr-3">{f.enabled ? <UsageBar used={f.used_today} limit={f.daily_limit} /> : '—'}</td>
                  <td className="py-3">{f.enabled ? <UsageBar used={f.used_this_month} limit={f.monthly_limit} /> : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>

        <div className="flex flex-col gap-6">
          <Card>
            <h2 className="text-sm font-semibold text-gray-900">Change password</h2>
            <form onSubmit={submitPassword} className="mt-3">
              <Field label="Current password" htmlFor="current">
                <PasswordInput id="current" autoComplete="current-password" value={current}
                  onChange={(e) => setCurrent(e.target.value)} required />
              </Field>
              <Field label="New password" htmlFor="new" hint="At least 10 characters.">
                <PasswordInput id="new" autoComplete="new-password" value={next}
                  onChange={(e) => setNext(e.target.value)} minLength={10} maxLength={128} required />
              </Field>
              {message ? (
                <div className="mt-3">
                  <Alert tone={message.tone}>{message.text}</Alert>
                </div>
              ) : null}
              <Button type="submit" className="mt-4" loading={busy}>
                Update password
              </Button>
            </form>
          </Card>

          <Card>
            <h2 className="text-sm font-semibold text-gray-900">Security</h2>
            <dl className="mt-3 space-y-1 text-sm text-gray-600">
              <div className="flex justify-between"><dt>Member since</dt><dd>{formatDate(me.created_at)}</dd></div>
              <div className="flex justify-between"><dt>Two-factor</dt><dd>{me.mfa_enabled ? 'On' : 'Off'}</dd></div>
            </dl>
            <div className="mt-4 flex flex-wrap gap-2">
              {me.role === 'admin' && me.mfa_enabled ? (
                <Button
                  variant="secondary"
                  onClick={() =>
                    void apiJson<{ recovery_codes: string[] }>('/auth/mfa/recovery-codes', 'POST').then((r) => setCodes(r.recovery_codes))
                  }
                >
                  New recovery codes
                </Button>
              ) : null}
              <Button variant="danger" onClick={() => void logoutAll()}>
                Sign out everywhere
              </Button>
            </div>
          </Card>
        </div>
      </div>

      <Modal open={codes !== null} title="New recovery codes" onClose={() => setCodes(null)}
        footer={<Button onClick={() => setCodes(null)}>I saved them</Button>}>
        <p className="text-sm text-gray-600">Your old codes no longer work. These are shown only once.</p>
        <ul className="mt-3 grid grid-cols-2 gap-2 font-mono text-sm">
          {(codes ?? []).map((c) => (
            <li key={c} className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-1.5 text-center">{c}</li>
          ))}
        </ul>
      </Modal>
    </div>
  )
}
