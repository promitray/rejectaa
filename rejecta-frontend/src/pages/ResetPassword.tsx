import type { FormEvent, ReactElement } from 'react'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { authPost } from '../auth/client'
import { Alert, Button, Field, PasswordInput } from '../components/ui'
import { AuthShell } from './Login'

export default function ResetPassword(): ReactElement {
  const [params] = useSearchParams()
  const token = params.get('token') ?? ''
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [busy, setBusy] = useState(false)

  function submit(event: FormEvent): void {
    event.preventDefault()
    setError(null)
    if (password !== confirm) {
      setError('The two passwords do not match.')
      return
    }
    setBusy(true)
    authPost<void>('/reset-password', { token, new_password: password })
      .then(() => setDone(true))
      .catch((err: unknown) => setError(err instanceof Error ? err.message : 'Could not set the password.'))
      .finally(() => setBusy(false))
  }

  if (!token) {
    return (
      <AuthShell title="Reset link missing">
        <Alert>This page needs the link your admin sent you.</Alert>
      </AuthShell>
    )
  }

  if (done) {
    return (
      <AuthShell title="Password set">
        <Alert tone="success">Your new password is ready. Sign in with it now.</Alert>
        <Link to="/login" className="mt-4 flex w-full justify-center rounded-lg bg-black px-4 py-2.5 text-sm font-medium text-white">
          Sign in →
        </Link>
      </AuthShell>
    )
  }

  return (
    <AuthShell title="Choose a new password" subtitle="This link works once. All your other sessions will be signed out.">
      <form onSubmit={submit}>
        <Field label="New password" htmlFor="password" hint="At least 10 characters.">
          <PasswordInput id="password" autoComplete="new-password" value={password}
            onChange={(e) => setPassword(e.target.value)} minLength={10} maxLength={128} required />
        </Field>
        <Field label="Repeat new password" htmlFor="confirm">
          <PasswordInput id="confirm" autoComplete="new-password" value={confirm}
            onChange={(e) => setConfirm(e.target.value)} required />
        </Field>
        <div className="mt-4">
          <Alert>{error}</Alert>
        </div>
        <Button type="submit" className="mt-4 w-full" loading={busy}>
          Set password →
        </Button>
      </form>
    </AuthShell>
  )
}
