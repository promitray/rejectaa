import type { FormEvent, ReactElement } from 'react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { adminApi } from '../../admin/api'
import type { PasswordLink } from '../../admin/api'
import { attempt, useLoad } from '../../admin/useLoad'
import { TableSkeleton } from '../../components/AdminSkeletons'
import { RefreshOverlay } from '../../components/Loaders'
import { Alert, Badge, Button, Card, CopyField, Field, Input, Modal, PageHeader, Pagination, Select, formatDate } from '../../components/ui'


export default function Users(): ReactElement {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [applied, setApplied] = useState('')
  const [role, setRole] = useState('')
  const [status, setStatus] = useState('')
  const [planId, setPlanId] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const plans = useLoad(() => adminApi.plans())
  const users = useLoad(
    () => adminApi.users({ search: applied, role, status, plan_id: planId, page, page_size: pageSize }),
    [applied, role, status, planId, page, pageSize],
  )

  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ email: '', full_name: '', role: 'user', plan_id: '' })
  const [createError, setCreateError] = useState<string | null>(null)
  const [link, setLink] = useState<PasswordLink | null>(null)
  const [busy, setBusy] = useState(false)

  function submitSearch(event: FormEvent): void {
    event.preventDefault()
    setPage(1)
    setApplied(search.trim())
  }

  async function createUser(): Promise<void> {
    setBusy(true)
    setCreateError(null)
    await attempt(async () => {
      const result = await adminApi.createUser({ ...form, plan_id: form.plan_id || null })
      setLink(result.password_setup)
      void users.reload()
    }, setCreateError)
    setBusy(false)
  }


  return (
    <div>
      <PageHeader
        title="Users"
        subtitle={users.data ? `${users.data.total} accounts` : undefined}
        actions={<Button onClick={() => { setCreating(true); setLink(null); setForm({ email: '', full_name: '', role: 'user', plan_id: '' }) }}>New user</Button>}
      />

      <Card className="mb-4">
        <form onSubmit={submitSearch} className="grid gap-3 sm:grid-cols-[1fr_auto_auto_auto_auto]">
          <Input placeholder="Search email or name" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search" />
          <Select value={role} onChange={(e) => { setRole(e.target.value); setPage(1) }} aria-label="Role">
            <option value="">All roles</option>
            <option value="user">Users</option>
            <option value="admin">Admins</option>
          </Select>
          <Select value={status} onChange={(e) => { setStatus(e.target.value); setPage(1) }} aria-label="Status">
            <option value="">Any status</option>
            <option value="active">Active</option>
            <option value="suspended">Suspended</option>
          </Select>
          <Select value={planId} onChange={(e) => { setPlanId(e.target.value); setPage(1) }} aria-label="Plan">
            <option value="">All plans</option>
            {(plans.data ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
          <Button type="submit" variant="secondary">Search</Button>
        </form>
      </Card>

      <Alert>{users.error}</Alert>
      {users.loading && !users.data ? <TableSkeleton rows={6} label="Loading users" /> : (
      <RefreshOverlay active={users.loading} label="Loading users">
      <div className="overflow-x-auto rounded-xl border border-gray-200 bg-white">
        <table className="w-full text-sm">
          <thead className="border-b border-gray-200 bg-gray-50 text-left text-xs text-gray-500">
            <tr>
              <th className="px-4 py-2 font-medium">User</th>
              <th className="px-4 py-2 font-medium">Plan</th>
              <th className="px-4 py-2 font-medium">Role</th>
              <th className="px-4 py-2 font-medium">Status</th>
              <th className="px-4 py-2 font-medium">Last sign-in</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {(users.data?.items ?? []).map((u) => (
              <tr key={u.id} className="cursor-pointer hover:bg-gray-50" onClick={() => navigate(`/admin/users/${u.id}`)}>
                <td className="px-4 py-2.5">
                  <Link to={`/admin/users/${u.id}`} className="font-medium text-gray-900" onClick={(e) => e.stopPropagation()}>
                    {u.email}
                  </Link>
                  {u.full_name ? <p className="text-xs text-gray-500">{u.full_name}</p> : null}
                </td>
                <td className="px-4 py-2.5">{u.plan.name}</td>
                <td className="px-4 py-2.5"><Badge tone={u.role === 'admin' ? 'black' : 'gray'}>{u.role}</Badge></td>
                <td className="px-4 py-2.5">
                  <Badge tone={u.status === 'active' ? 'green' : 'red'}>{u.status}</Badge>{' '}
                  {u.locked ? <Badge tone="amber">locked</Badge> : null}
                </td>
                <td className="px-4 py-2.5 text-gray-600">{formatDate(u.last_login_at)}</td>
              </tr>
            ))}
            {users.data && users.data.items.length === 0 ? (
              <tr><td colSpan={5} className="px-4 py-6 text-center text-gray-500">No users match.</td></tr>
            ) : null}
          </tbody>
        </table>
      </div>
      </RefreshOverlay>
      )}
      {users.data ? (
        <Pagination
          page={page}
          pageSize={pageSize}
          total={users.data.total}
          busy={users.loading}
          onPage={setPage}
          onPageSize={(size) => { setPageSize(size); setPage(1) }}
        />
      ) : null}

      <Modal
        open={creating}
        title={link ? 'User created' : 'New user'}
        onClose={() => setCreating(false)}
        footer={link ? <Button onClick={() => setCreating(false)}>Done</Button> : (
          <>
            <Button variant="secondary" onClick={() => setCreating(false)}>Cancel</Button>
            <Button loading={busy} onClick={() => void createUser()} disabled={!form.email}>Create</Button>
          </>
        )}
      >
        {link ? (
          <div>
            <Alert tone="success">Send this one-time link to the person so they can set their password. It expires {formatDate(link.expires_at)} and is shown only now.</Alert>
            <div className="mt-3"><CopyField value={link.url} label="Password setup link" /></div>
          </div>
        ) : (
          <div>
            <Field label="Email" htmlFor="new-email">
              <Input id="new-email" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
            </Field>
            <Field label="Full name" htmlFor="new-name">
              <Input id="new-name" value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} />
            </Field>
            <Field label="Role" htmlFor="new-role" hint="Admins start on the Lab plan (unlimited) and must set up two-factor sign-in at first login.">
              <Select id="new-role" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
                <option value="user">User</option>
                <option value="admin">Admin</option>
              </Select>
            </Field>
            <Field label="Plan" htmlFor="new-plan">
              <Select id="new-plan" value={form.plan_id} onChange={(e) => setForm({ ...form, plan_id: e.target.value })}>
                <option value="">Default plan</option>
                {(plans.data ?? []).filter((p) => p.status === 'active').map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </Select>
            </Field>
            <div className="mt-4"><Alert>{createError}</Alert></div>
          </div>
        )}
      </Modal>
    </div>
  )
}
