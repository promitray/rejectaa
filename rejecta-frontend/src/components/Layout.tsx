import type { ReactElement, ReactNode } from 'react'
import { Link } from 'react-router-dom'

interface LayoutProps {
  children: ReactNode
}

const marketingUrl = import.meta.env.VITE_MARKETING_URL

export default function Layout({ children }: LayoutProps): ReactElement {
  return (
    <div className="min-h-screen bg-white text-gray-900">
      <header className="border-b border-gray-200">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <Link to="/" className="flex items-center gap-2 text-base font-semibold tracking-tight">
            <span className="h-2 w-2 rounded-full bg-red-600" aria-hidden="true" />
            Rejecta
          </Link>
          <nav className="flex items-center gap-4">
            {marketingUrl ? (
              <a href={marketingUrl} className="text-sm text-gray-600 hover:text-gray-900">
                How it works
              </a>
            ) : (
              <a href="/#how-it-works" className="text-sm text-gray-600 hover:text-gray-900">
                How it works
              </a>
            )}
          </nav>
        </div>
      </header>
      <main>{children}</main>
    </div>
  )
}
