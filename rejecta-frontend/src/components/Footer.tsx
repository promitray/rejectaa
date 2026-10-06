import type { ReactElement } from 'react'
import { Link } from 'react-router-dom'

export default function Footer(): ReactElement {
  return (
    <footer className="border-t border-gray-200 print:hidden">
      <div className="mx-auto flex max-w-5xl flex-col gap-3 px-4 py-8 text-xs leading-5 text-gray-500 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="flex items-center gap-2 font-semibold text-gray-800">
            <span className="h-1.5 w-1.5 rounded-full bg-red-600" aria-hidden="true" />
            Rejecta
          </p>
          <p className="mt-2 max-w-md">
            A pre-submission desk review. Not a peer review, not a journal decision, and not a
            grammar checker. Manuscripts are parsed in memory and are not stored.
          </p>
        </div>
        <div className="flex gap-4">
          <Link to="/" className="hover:text-gray-800">
            Home
          </Link>
          <Link to="/app" className="hover:text-gray-800">
            Try it now
          </Link>
          <Link to="/#how-it-works" className="hover:text-gray-800">
            How it works
          </Link>
        </div>
      </div>
    </footer>
  )
}
