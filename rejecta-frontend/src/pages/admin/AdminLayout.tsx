import type { ReactElement } from 'react'
import { Suspense } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { SectionSkeleton } from '../../components/AdminSkeletons'

const LINKS = [
  { to: '/admin', label: 'Overview', end: true },
  { to: '/admin/users', label: 'Users' },
  { to: '/admin/plans', label: 'Plans' },
  { to: '/admin/features', label: 'Features' },
  { to: '/admin/partners', label: 'Partners' },
  { to: '/admin/usage', label: 'Usage' },
  { to: '/admin/audit', label: 'Audit log' },
]

export default function AdminLayout(): ReactElement {
  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-8 md:flex-row">
      <aside className="md:w-48 md:shrink-0">
        <p className="mb-2 flex items-center gap-2 px-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
          <span className="h-1.5 w-1.5 rounded-full bg-red-600" aria-hidden="true" />
          Admin
        </p>
        <nav className="flex gap-1 overflow-x-auto md:flex-col">
          {LINKS.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              end={link.end}
              className={({ isActive }) =>
                `whitespace-nowrap rounded-lg px-2.5 py-1.5 text-sm ${
                  isActive ? 'bg-gray-900 text-white' : 'text-gray-600 hover:bg-gray-100 hover:text-gray-900'
                }`
              }
            >
              {link.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <section className="min-w-0 flex-1">
        <Suspense fallback={<SectionSkeleton />}>
          <Outlet />
        </Suspense>
      </section>
    </div>
  )
}
