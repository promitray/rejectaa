import type { ReactElement } from 'react'
import { Link, Outlet } from 'react-router-dom'
import Footer from './Footer'

export default function Layout(): ReactElement {
  return (
    <div className="min-h-screen bg-white text-gray-900">
      <header className="border-b border-gray-200 print:hidden">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <Link to="/" className="flex items-center gap-2 text-base font-semibold tracking-tight">
            <span className="h-2 w-2 rounded-full bg-red-600" aria-hidden="true" />
            Rejecta
          </Link>
          <nav className="flex items-center gap-4">
            <Link to="/#how-it-works" className="text-sm text-gray-600 hover:text-gray-900">
              How it works
            </Link>
            <Link to="/app" className="text-sm text-gray-600 hover:text-gray-900">
              Desk
            </Link>
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
