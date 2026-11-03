import type { ReactElement } from 'react'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { adminApi } from '../../admin/api'
import type { AdminUserDetail, PasswordLink, UserOverride } from '../../admin/api'
import { attempt, useLoad } from '../../admin/useLoad'
import { useAuth } from '../../auth/AuthProvider'
import type { FeatureUsage } from '../../auth/types'
import {
  Alert, Badge, Button, Card, CopyField, Field, Input, Modal, PageHeader, Select, UsageBar, formatDate,
} from '../../components/ui'
import { DetailSkeleton } from '../../components/AdminSkeletons'
import { RefreshOverlay } from '../../components/Loaders'
import LimitInput from './LimitInput'

type Inherit = 'inherit' | 'on' | 'off'

interface OverrideForm {
  enabled: Inherit
  dailySet: boolean
  daily: number | null
  monthlySet: boolean
  monthly: number | null
  expires: string
  reason: string
}

/** ISO timestamp -> value for <input type="datetime-local"> in the browser's timezone. */
function toLocalInput(iso: string): string {
  const d = new Date(iso)
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}

function toForm(o: UserOverride | undefined, f: FeatureUsage): OverrideForm {
  return {
    enabled: o?.enabled === null || o === undefined ? 'inherit' : o.enabled ? 'on' : 'off',
    dailySet: o?.daily_limit_set ?? false,
    daily: o?.daily_limit_set ? o.daily_limit : f.daily_limit,
    monthlySet: o?.monthly_limit_set ?? false,
    monthly: o?.monthly_limit_set ? o.monthly_limit : f.monthly_limit,
    expires: o?.expires_at ? toLocalInput(o.expires_at) : '',
    reason: o?.reason ?? '',
  }
}

export default function UserDetail(): ReactElement {
  const { id = '' } = useParams()
  const { me } = useAuth()
  const user = useLoad(() => adminApi.user(id), [id])
  const plans = useLoad(() => adminApi.plans())
  const [profile, setProfile] = useState({ full_name: '', role: 'user', status: 'active', plan_id: '' })
  const [message, setMessage] = useState<{ tone: 'error' | 'success'; text: string } | null>(null)
  const [link, setLink] = useState<PasswordLink | null>(null)
  const [editing, setEditing] = useState<FeatureUsage | null>(null)
  const [form, setForm] = useState<OverrideForm | null>(null)
  const [busy, setBusy] = useState(false)

  const u: AdminUserDetail | null = user.data
  useEffect(() => {
    if (u) setProfile({ full_name: u.full_name, role: u.role, status: u.status, plan_id: u.plan.id })
  }, [u])

  if (!u) {
    return user.error ? <Alert>{user.error}</Alert> : <DetailSkeleton label="Loading user" />
  }
  const isSelf = me?.id === u.id
  const fail = (text: string): void => setMessage({ tone: 'error', text })
  const ok = (text: string): void => setMessage({ tone: 'success', text })

  async function act(action: () => Promise<unknown>, success: string, confirmText?: string): Promise<void> {
    if (confirmText && !window.confirm(confirmText)) return
    setBusy(true)
    setMessage(null)
    if (await attempt(action, fail)) {
      ok(success)
      await user.reload()
    }
    setBusy(false)
  }

  function saveProfile(): void {
    if (!u) return
    const changes: Record<string, string> = {}
    if (profile.full_name !== u.full_name) changes.full_name = profile.full_name
    if (profile.role !== u.role) changes.role = profile.role
    if (profile.status !== u.status) changes.status = profile.status
    if (profile.plan_id !== u.plan.id) changes.plan_id = profile.plan_id
    if (Object.keys(changes).length === 0) return ok('Nothing changed.')
    const signsOut = 'role' in changes || 'status' in changes
    void act(() => adminApi.updateUser(u.id, changes), signsOut ? 'Saved. The user was signed out everywhere.' : 'Saved.')
  }

  function openOverride(f: FeatureUsage): void {
    setEditing(f)
    setForm(toForm(u?.overrides.find((o) => o.feature_key === f.key), f))
  }

  function saveOverride(): void {
    if (!editing || !form || !u) return
    const body = {
      enabled: form.enabled === 'inherit' ? null : form.enabled === 'on',
      daily_limit_set: form.dailySet,
      daily_limit: form.dailySet ? form.daily : null,
      monthly_limit_set: form.monthlySet,
      monthly_limit: form.monthlySet ? form.monthly : null,
      expires_at: form.expires ? new Date(form.expires).toISOString() : null,
      reason: form.reason,
    }
    void act(() => adminApi.setOverride(u.id, editing.key, body), `Override saved for ${editing.name}.`).then(() => setEditing(null))
  }

  return (
    <div>
      <p className="mb-2 text-xs text-gray-500"><Link to="/admin/users" className="hover:underline">Users</Link> /</p>
      <PageHeader
        title={u.email}
        subtitle={<>
          <Badge tone={u.role === 'admin' ? 'black' : 'gray'}>{u.role}</Badge>{' '}
          <Badge tone={u.status === 'active' ? 'green' : 'red'}>{u.status}</Badge>{' '}
          {u.locked ? <Badge tone="amber">locked after failed sign-ins</Badge> : null}{' '}
          <span className="text-xs">Joined {formatDate(u.created_at)} · last sign-in {formatDate(u.last_login_at)}</span>
        </>}
      />
      {message ? <div className="mb-4"><Alert tone={message.tone}>{message.text}</Alert></div> : null}

      <RefreshOverlay active={user.loading} label="Updating user">
      <div className="grid gap-6 lg:grid-cols-[1fr_1.6fr]">
        <div className="flex flex-col gap-6">
          <Card>
            <h2 className="text-sm font-semibold text-gray-900">Profile & access</h2>
            <div className="mt-3">
              <Field label="Full name" htmlFor="u-name">
                <Input id="u-name" value={profile.full_name} onChange={(e) => setProfile({ ...profile, full_name: e.target.value })} />
              </Field>
              <Field label="Plan" htmlFor="u-plan">
                <Select id="u-plan" value={profile.plan_id} onChange={(e) => setProfile({ ...profile, plan_id: e.target.value })}>
                  {(plans.data ?? []).filter((p) => p.status === 'active' || p.id === u.plan.id).map((p) => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </Select>
              </Field>
              <Field label="Role" htmlFor="u-role" hint={isSelf ? 'You cannot change your own role.' : undefined}>
                <Select id="u-role" value={profile.role} disabled={isSelf} onChange={(e) => setProfile({ ...profile, role: e.target.value })}>
                  <option value="user">User</option>
                  <option value="admin">Admin</option>
                </Select>
              </Field>
              <Field label="Status" htmlFor="u-status">
                <Select id="u-status" value={profile.status} disabled={isSelf} onChange={(e) => setProfile({ ...profile, status: e.target.value })}>
                  <option value="active">Active</option>
                  <option value="suspended">Suspended</option>
                </Select>
              </Field>
              <Button className="mt-4" loading={busy} onClick={saveProfile}>Save</Button>
            </div>
          </Card>

          <Card>
            <h2 className="text-sm font-semibold text-gray-900">Security</h2>
            <dl className="mt-3 space-y-1 text-sm text-gray-600">
              <div className="flex justify-between"><dt>Active sessions</dt><dd>{u.active_sessions}</dd></div>
              <div className="flex justify-between"><dt>Two-factor</dt><dd>{u.mfa_enabled ? 'On' : 'Off'}</dd></div>
              {u.recovery_codes_left !== null ? (
                <div className="flex justify-between"><dt>Recovery codes left</dt><dd>{u.recovery_codes_left}</dd></div>
              ) : null}
            </dl>
            <div className="mt-4 flex flex-wrap gap-2">
              <Button variant="secondary" disabled={busy}
                onClick={() => void attempt(async () => setLink(await adminApi.resetLink(u.id)), fail)}>
                Password reset link
              </Button>
              <Button variant="secondary" disabled={busy || u.active_sessions === 0}
                onClick={() => void act(() => adminApi.revokeSessions(u.id), 'Signed out everywhere.', 'Sign this user out on every device?')}>
                Sign out everywhere
              </Button>
              {u.mfa_enabled ? (
                <Button variant="danger" disabled={busy}
                  onClick={() => void act(() => adminApi.resetMfa(u.id), 'Two-factor removed. They set it up again at next sign-in.', 'Remove two-factor for this user? Use this only if they lost their device.')}>
                  Reset two-factor
                </Button>
              ) : null}
            </div>
          </Card>
        </div>

        <Card>
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-900">Features & usage</h2>
            <Button variant="ghost" onClick={() => void act(() => adminApi.resetUsage(u.id, {}), 'Usage counters reset.', 'Reset today\'s and this month\'s usage for every feature?')}>
              Reset all usage
            </Button>
          </div>
          <p className="mt-1 text-xs text-gray-500">Limits come from the {u.plan.name} plan unless an override is set.</p>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-gray-500">
                <tr><th className="pb-2 font-medium">Feature</th><th className="pb-2 font-medium">Today</th><th className="pb-2 font-medium">Month</th><th /></tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {u.features.map((f) => {
                  const override = u.overrides.find((o) => o.feature_key === f.key)
                  return (
                    <tr key={f.key}>
                      <td className="py-3 pr-3">
                        <p className="font-medium text-gray-900">{f.name}</p>
                        <p className="text-xs text-gray-500">
                          {f.enabled ? 'On' : f.reason === 'feature_disabled' ? 'Off for everyone' : 'Off'}
                          {override ? <> · <Badge tone="amber">override</Badge>{override.expires_at ? ` until ${formatDate(override.expires_at)}` : ''}</> : null}
                        </p>
                      </td>
                      <td className="py-3 pr-3"><UsageBar used={f.used_today} limit={f.daily_limit} /></td>
                      <td className="py-3 pr-3"><UsageBar used={f.used_this_month} limit={f.monthly_limit} /></td>
                      <td className="py-3 text-right">
                        <div className="flex justify-end gap-1">
                          <Button variant="secondary" onClick={() => openOverride(f)}>Override</Button>
                          {override ? (
                            <Button variant="ghost" onClick={() => void act(() => adminApi.deleteOverride(u.id, f.key), 'Override removed.')}>Remove</Button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
      </RefreshOverlay>

      <Modal open={link !== null} title="Password reset link" onClose={() => setLink(null)} footer={<Button onClick={() => setLink(null)}>Done</Button>}>
        {link ? (
          <>
            <p className="text-sm text-gray-600">Send this to the user. It works once, until {formatDate(link.expires_at)}. Older links stop working.</p>
            <div className="mt-3"><CopyField value={link.url} label="Reset link" /></div>
          </>
        ) : null}
      </Modal>

      <Modal
        open={editing !== null && form !== null}
        title={`Override: ${editing?.name ?? ''}`}
        onClose={() => setEditing(null)}
        footer={<><Button variant="secondary" onClick={() => setEditing(null)}>Cancel</Button><Button loading={busy} onClick={saveOverride}>Save override</Button></>}
      >
        {form ? (
          <div>
            <Field label="Access" htmlFor="o-enabled">
              <Select id="o-enabled" value={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.value as Inherit })}>
                <option value="inherit">Same as plan</option>
                <option value="on">Always on</option>
                <option value="off">Turned off for this user</option>
              </Select>
            </Field>
            <Field label="Daily limit">
              <label className="mb-2 flex items-center gap-2 text-sm text-gray-700">
                <input type="checkbox" checked={form.dailySet} onChange={(e) => setForm({ ...form, dailySet: e.target.checked })} />
                Override the plan's daily limit
              </label>
              {form.dailySet ? <LimitInput value={form.daily} onChange={(v) => setForm({ ...form, daily: v })} label="Daily limit" /> : null}
            </Field>
            <Field label="Monthly limit">
              <label className="mb-2 flex items-center gap-2 text-sm text-gray-700">
                <input type="checkbox" checked={form.monthlySet} onChange={(e) => setForm({ ...form, monthlySet: e.target.checked })} />
                Override the plan's monthly limit
              </label>
              {form.monthlySet ? <LimitInput value={form.monthly} onChange={(v) => setForm({ ...form, monthly: v })} label="Monthly limit" /> : null}
            </Field>
            <Field label="Expires" htmlFor="o-expires" hint="Leave empty for no expiry.">
              <Input id="o-expires" type="datetime-local" value={form.expires} onChange={(e) => setForm({ ...form, expires: e.target.value })} />
            </Field>
            <Field label="Reason" htmlFor="o-reason">
              <Input id="o-reason" value={form.reason} maxLength={500} onChange={(e) => setForm({ ...form, reason: e.target.value })} placeholder="e.g. trial for lab pilot" />
            </Field>
          </div>
        ) : null}
      </Modal>
    </div>
  )
}
