import type { FormEvent, ReactElement } from 'react'
import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { QRCodeSVG } from 'qrcode.react'
import { useAuth } from '../auth/AuthProvider'
import { ApiError, peekSignInNotice } from '../auth/client'
import type { MfaChallenge, MfaEnrollStart } from '../auth/types'
import { Alert, Button, CopyField, Field, Input, PasswordInput } from '../components/ui'

type Step = 'credentials' | 'verify' | 'enroll' | 'codes'

export function AuthShell({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }): ReactElement {
  return (
    <div className="px-4 py-12">
      <div className="mx-auto max-w-md">
        <h1 className="text-center text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle ? <p className="mt-2 text-center text-sm leading-6 text-gray-600">{subtitle}</p> : null}
        <div className="mt-8 rounded-xl border border-gray-200 bg-white p-6 shadow-sm">{children}</div>
      </div>
    </div>
  )
}

function safeNext(value: string | null): string | null {
  // Only same-site paths, never "//evil.com" or absolute URLs.
  return value && value.startsWith('/') && !value.startsWith('//') ? value : null
}

export default function Login(): ReactElement {
  const auth = useAuth()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const next = safeNext(params.get('next'))

  const [step, setStep] = useState<Step>('credentials')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [challenge, setChallenge] = useState<MfaChallenge | null>(null)
  const [enrollment, setEnrollment] = useState<MfaEnrollStart | null>(null)
  const [code, setCode] = useState('')
  const [useRecovery, setUseRecovery] = useState(false)
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice] = useState(() => peekSignInNotice())

  // Enrollment stores the session but loads the profile only after the codes
  // were saved ("continue"), so this never redirects away from that screen.
  useEffect(() => {
    if (auth.status === 'signedIn' && auth.me) {
      navigate(next ?? (auth.me.role === 'admin' ? '/admin' : '/app'), { replace: true })
    }
  }, [auth.status, auth.me, next, navigate])

  async function run(action: () => Promise<void>): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (err) {
      if (err instanceof ApiError && err.code === 'invalid_mfa_token') {
        setStep('credentials')
        setChallenge(null)
        setCode('')
      }
      setError(err instanceof Error ? err.message : 'Something went wrong.')
    } finally {
      setBusy(false)
    }
  }

  function submitCredentials(event: FormEvent): void {
    event.preventDefault()
    void run(async () => {
      const result = await auth.login(email.trim(), password)
      if (!result) return
      setChallenge(result)
      setPassword('')
      if (result.mfa_enrollment_required) {
        setEnrollment(await auth.mfaEnrollStart(result.mfa_token))
        setStep('enroll')
      } else {
        setStep('verify')
      }
    })
  }

  function submitCode(event: FormEvent): void {
    event.preventDefault()
    if (!challenge) return
    void run(async () => {
      if (step === 'enroll') {
        setRecoveryCodes(await auth.mfaEnrollConfirm(challenge.mfa_token, code.trim()))
        setStep('codes')
      } else {
        await auth.mfaVerify(challenge.mfa_token, useRecovery ? { recovery_code: code.trim() } : { code: code.trim() })
      }
    })
  }

  if (step === 'codes') {
    return (
      <AuthShell title="Save your recovery codes" subtitle="Each code works once if you lose your authenticator. They are shown only now.">
        <ul className="grid grid-cols-2 gap-2 font-mono text-sm">
          {recoveryCodes.map((c) => (
            <li key={c} className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-1.5 text-center">
              {c}
            </li>
          ))}
        </ul>
        <div className="mt-4 flex gap-2">
          <Button variant="secondary" className="flex-1" onClick={() => void navigator.clipboard.writeText(recoveryCodes.join('\n'))}>
            Copy all
          </Button>
          <Button
            variant="secondary"
            className="flex-1"
            onClick={() => {
              const blob = new Blob([`Rejecta recovery codes for ${email}\n\n${recoveryCodes.join('\n')}\n`], { type: 'text/plain' })
              const link = document.createElement('a')
              link.href = URL.createObjectURL(blob)
              link.download = 'rejecta-recovery-codes.txt'
              link.click()
              URL.revokeObjectURL(link.href)
            }}
          >
            Download
          </Button>
        </div>
        <Button className="mt-4 w-full" loading={busy} onClick={() => void run(() => auth.reloadMe())}>
          I saved them, continue →
        </Button>
      </AuthShell>
    )
  }

  if (step === 'enroll' || step === 'verify') {
    return (
      <AuthShell
        title={step === 'enroll' ? 'Set up two-factor sign-in' : 'Two-factor sign-in'}
        subtitle={
          step === 'enroll'
            ? 'Admin accounts need an authenticator app (Google Authenticator, 1Password, Authy…).'
            : 'Enter the 6-digit code from your authenticator app.'
        }
      >
        <form onSubmit={submitCode}>
          {step === 'enroll' && enrollment ? (
            <div className="mb-5 flex flex-col items-center gap-3">
              <div className="rounded-lg border border-gray-200 p-3">
                <QRCodeSVG value={enrollment.otpauth_uri} size={176} />
              </div>
              <p className="text-center text-xs text-gray-500">Scan the code, or enter this key manually:</p>
              <CopyField value={enrollment.secret} label="Authenticator setup key" />
            </div>
          ) : null}
          <Field label={useRecovery ? 'Recovery code' : 'Authenticator code'} htmlFor="code">
            <Input
              id="code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              inputMode={useRecovery ? 'text' : 'numeric'}
              autoComplete="one-time-code"
              placeholder={useRecovery ? 'ABCDE-FGHJK' : '123456'}
              autoFocus
              required
            />
          </Field>
          <div className="mt-4">
            <Alert>{error}</Alert>
          </div>
          <Button type="submit" className="mt-4 w-full" loading={busy}>
            {step === 'enroll' ? 'Turn on two-factor →' : 'Verify →'}
          </Button>
          {step === 'verify' ? (
            <button
              type="button"
              className="mt-3 w-full text-center text-xs text-gray-600 underline-offset-2 hover:underline"
              onClick={() => {
                setUseRecovery(!useRecovery)
                setCode('')
              }}
            >
              {useRecovery ? 'Use authenticator code instead' : 'Lost your phone? Use a recovery code'}
            </button>
          ) : null}
        </form>
      </AuthShell>
    )
  }

  return (
    <AuthShell title="Sign in" subtitle="Welcome back to Rejecta.">
      {notice ? (
        <div className="mb-4">
          <Alert tone="info">{notice}</Alert>
        </div>
      ) : null}
      <form onSubmit={submitCredentials}>
        <Field label="Email" htmlFor="email">
          <Input id="email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </Field>
        <Field label="Password" htmlFor="password">
          <PasswordInput
            id="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </Field>
        <div className="mt-4">
          <Alert>{error}</Alert>
        </div>
        <Button type="submit" className="mt-4 w-full" loading={busy}>
          Sign in →
        </Button>
      </form>
      <p className="mt-4 text-center text-xs text-gray-500">
        No account?{' '}
        <Link to={`/signup${next ? `?next=${encodeURIComponent(next)}` : ''}`} className="text-gray-900 underline-offset-2 hover:underline">
          Create one
        </Link>
        <span className="mx-1">·</span>
        Forgot your password? Ask your Rejecta admin for a reset link.
      </p>
    </AuthShell>
  )
}
