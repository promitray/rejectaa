import type { ReactElement } from 'react'
import { useEffect, useRef, useState } from 'react'
import { Link, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthProvider'
import Footer from './Footer'

function UserMenu(): ReactElement {
  const { status, me, logout } = useAuth()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onClick = (event: MouseEvent): void => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [])

  if (status === 'loading' || (status === 'signedIn' && !me)) {
    return <span className="h-8 w-8" aria-hidden="true" />
  }
  if (status !== 'signedIn' || !me) {
    return (
      <div className="flex items-center gap-3">
        <Link to="/login" className="text-sm text-gray-600 hover:text-gray-900">
          Sign in
        </Link>
        <Link to="/signup" className="rounded-lg bg-black px-3 py-1.5 text-sm font-medium text-white hover:bg-gray-800">
          Sign up
        </Link>
      </div>
    )
  }

  const initial = (me.full_name || me.email).charAt(0).toUpperCase()
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex h-8 w-8 items-center justify-center rounded-full bg-gray-900 text-sm font-medium text-white"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Account menu"
      >
        {initial}
      </button>
      {open ? (
        <div className="absolute right-0 z-40 mt-2 w-56 rounded-xl border border-gray-200 bg-white py-1 text-sm shadow-lg" role="menu">
          <div className="border-b border-gray-100 px-3 py-2">
            <p className="truncate font-medium text-gray-900">{me.full_name || me.email}</p>
            <p className="truncate text-xs text-gray-500">{me.plan?.name} plan</p>
          </div>
          <Link to="/account" onClick={() => setOpen(false)} className="block px-3 py-2 text-gray-700 hover:bg-gray-50" role="menuitem">
            Account & usage
          </Link>
          {me.role === 'admin' ? (
            <Link to="/admin" onClick={() => setOpen(false)} className="block px-3 py-2 text-gray-700 hover:bg-gray-50" role="menuitem">
              Admin
            </Link>
          ) : null}
          <button
            type="button"
            role="menuitem"
            className="block w-full px-3 py-2 text-left text-gray-700 hover:bg-gray-50"
            onClick={() => {
              setOpen(false)
              void logout().then(() => navigate('/'))
            }}
          >
            Sign out
          </button>
        </div>
      ) : null}
    </div>
  )
}

export default function Layout(): ReactElement {
  return (
    <div className="min-h-screen bg-white text-gray-900">
      <header className="border-b border-gray-200 print:hidden">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-4 px-4 py-3">
          <Link to="/" className="flex items-center gap-2 text-base font-semibold tracking-tight">
            <span className="h-2 w-2 rounded-full bg-red-600" aria-hidden="true" />
            Rejecta
          </Link>
          <nav className="flex flex-wrap items-center justify-end gap-4">
            <Link to="/#how-it-works" className="hidden text-sm text-gray-600 hover:text-gray-900 md:inline">
              How it works
            </Link>
            <Link to="/app" className="text-sm text-gray-600 hover:text-gray-900">
              Desk
            </Link>
            <Link to="/calibrate" className="hidden text-sm font-medium text-stone-700 hover:text-stone-900 sm:inline">
              Voice Calibrator
            </Link>
            <Link to="/heatmap" className="hidden text-sm font-medium text-stone-700 hover:text-stone-900 sm:inline">
              AI Signal Heatmap
            </Link>
            <Link to="/forge" className="hidden text-sm font-medium text-stone-700 hover:text-stone-900 lg:inline">
              Abstract Forge
            </Link>
            <Link to="/rebuttal" className="hidden text-sm font-medium text-stone-900 hover:text-stone-700 lg:inline">
              Rebuttal Writer
            </Link>
            <UserMenu />
          </nav>
        </div>
      </header>
      <main>
        <Outlet />
      </main>
      <Footer />
    </div>
  )
}
