import type { ReactElement } from 'react'
import { Link } from 'react-router-dom'
import { adminApi } from '../../admin/api'
import { useLoad } from '../../admin/useLoad'
import { OverviewSkeleton } from '../../components/AdminSkeletons'
import { Alert, Card, PageHeader } from '../../components/ui'

function Stat({ label, value, to }: { label: string; value: number; to?: string }): ReactElement {
  const body = (
    <Card className="h-full">
      <p className="text-xs text-gray-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight text-gray-900">{value.toLocaleString()}</p>
    </Card>
  )
  return to ? <Link to={to} className="block hover:opacity-90">{body}</Link> : body
}

export default function Overview(): ReactElement {
  const { data, error, loading } = useLoad(() => adminApi.stats())

  return (
    <div>
      <PageHeader title="Overview" subtitle="Accounts, plans and usage across Rejecta." />
      <Alert>{error}</Alert>
      {loading && !data ? <OverviewSkeleton /> : null}
      {data ? (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat label="Users" value={data.users_total} to="/admin/users" />
            <Stat label="Active" value={data.users_active} />
            <Stat label="Suspended" value={data.users_suspended} />
            <Stat label="Admins" value={data.admins} />
            <Stat label="Sign-ups, last 7 days" value={data.signups_last_7_days} />
            <Stat label="Sign-ins today" value={data.logins_today} />
            <Stat label="Tool runs today" value={data.usage_today} to="/admin/usage" />
            <Stat label="Tool runs this month" value={data.usage_this_month} to="/admin/usage" />
          </div>
          <Card className="mt-6">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-gray-900">Users by plan</h2>
              <Link to="/admin/plans" className="text-xs text-gray-600 underline-offset-2 hover:underline">
                Manage plans
              </Link>
            </div>
            <ul className="mt-3 divide-y divide-gray-100 text-sm">
              {data.users_by_plan.map((p) => (
                <li key={p.key} className="flex items-center justify-between py-2">
                  <span className={p.status === 'archived' ? 'text-gray-400' : 'text-gray-800'}>
                    {p.name} {p.status === 'archived' ? '(archived)' : ''}
                  </span>
                  <span className="font-medium">{p.users}</span>
                </li>
              ))}
            </ul>
          </Card>
        </>
      ) : null}
    </div>
  )
}
