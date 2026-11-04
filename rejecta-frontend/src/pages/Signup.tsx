import type { FormEvent, ReactElement } from 'react'
import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../auth/AuthProvider'
import { Alert, Button, Field, Input, PasswordInput } from '../components/ui'
import { AuthShell } from './Login'

export default function Signup(): ReactElement {
  const auth = useAuth()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const nextParam = params.get('next')
  const next = nextParam && nextParam.startsWith('/') && !nextParam.startsWith('//') ? nextParam : '/app'

  const [fullName, setFullName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (auth.status === 'signedIn') navigate(next, { replace: true })
  }, [auth.status, next, navigate])

  function submit(event: FormEvent): void {
    event.preventDefault()
    setError(null)
    if (password.length < 10) {
      setError('Password must be at least 10 characters.')
      return
    }
    setBusy(true)
    auth
      .signup(email.trim(), password, fullName.trim())
      .catch((err: unknown) => setError(err instanceof Error ? err.message : 'Sign-up failed.'))
      .finally(() => setBusy(false))
  }

  return (
    <AuthShell title="Create your account" subtitle="Free plan included. Upgrade limits any time with your admin.">
      <form onSubmit={submit}>
        <Field label="Full name" htmlFor="name">
          <Input id="name" autoComplete="name" value={fullName} onChange={(e) => setFullName(e.target.value)} maxLength={120} />
        </Field>
        <Field label="Email" htmlFor="email">
          <Input id="email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </Field>
        <Field label="Password" htmlFor="password" hint="At least 10 characters. A short sentence works well.">
          <PasswordInput
            id="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={10}
            maxLength={128}
            required
          />
        </Field>
        <div className="mt-4">
          <Alert>{error}</Alert>
        </div>
        <Button type="submit" className="mt-4 w-full" loading={busy}>
          Create account →
        </Button>
      </form>
      <p className="mt-4 text-center text-xs text-gray-500">
        Already have an account?{' '}
        <Link to="/login" className="text-gray-900 underline-offset-2 hover:underline">
          Sign in
        </Link>
      </p>
    </AuthShell>
  )
}
