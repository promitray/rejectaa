import type { ReactElement } from 'react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import Frowns from '../components/Frowns'
import { useRejectaStore } from '../store'
import type { Mood } from '../types'

interface Step {
  label: string
  subtitle: string
  at: number
}

const STEPS: Step[] = [
  { at: 0, label: 'Parsing document', subtitle: 'Reading the PDF structure' },
  { at: 3, label: 'Extracting sections and references', subtitle: 'Finding the abstract, sections, and bibliography' },
  { at: 6, label: 'Verifying citations against CrossRef', subtitle: 'Checking each reference for a real paper' },
  { at: 12, label: 'Looking up journal scope on OpenAlex', subtitle: 'Matching the journal and recent papers' },
  { at: 15, label: 'Professor Frowns reviewing', subtitle: 'This usually takes under a minute' },
  { at: -1, label: 'Writing the verdict', subtitle: 'Putting the letter together' },
]

type StepState = 'pending' | 'active' | 'done'

function moodForElapsed(elapsed: number): Mood {
  if (elapsed < 4) return 'neutral'
  if (elapsed < 8) return 'unimpressed'
  if (elapsed < 20) return 'skeptical'
  if (elapsed < 28) return 'unimpressed'
  return 'skeptical'
}

function stepState(index: number, elapsed: number, complete: boolean): StepState {
  if (index === STEPS.length - 1) {
    return complete ? 'done' : 'pending'
  }
  const start = STEPS[index]?.at ?? 0
  const next = STEPS[index + 1]
  if (elapsed < start) return 'pending'
  if (!next || next.at < 0 || elapsed < next.at) {
    return complete ? 'done' : 'active'
  }
  return 'done'
}

function activeCopy(elapsed: number, complete: boolean): Step {
  if (complete) {
    return STEPS[STEPS.length - 1] ?? STEPS[0]
  }
  let current = STEPS[0]
  for (const step of STEPS) {
    if (step.at < 0) continue
    if (elapsed >= step.at) current = step
  }
  return current
}

export default function Processing(): ReactElement {
  const navigate = useNavigate()
  const result = useRejectaStore((state) => state.result)
  const loading = useRejectaStore((state) => state.loading)
  const error = useRejectaStore((state) => state.error)
  const reset = useRejectaStore((state) => state.reset)
  const [elapsed, setElapsed] = useState(0)
  const [complete, setComplete] = useState(false)

  useEffect(() => {
    if (result) {
      navigate('/results', { replace: true })
      return
    }
    if (!loading && !error) {
      navigate('/app', { replace: true })
    }
  }, [result, loading, error, navigate])

  useEffect(() => {
    const started = Date.now()
    const id = window.setInterval(() => {
      setElapsed((Date.now() - started) / 1000)
      const snapshot = useRejectaStore.getState()
      if (snapshot.result) {
        setComplete(true)
        navigate('/results', { replace: true })
      }
    }, 200)
    return () => window.clearInterval(id)
  }, [navigate])

  const copy = activeCopy(elapsed, complete)
  const mood = moodForElapsed(elapsed)

  if (error) {
    return (
      <div className="mx-auto max-w-md px-4 pt-16 text-center">
        <Frowns mood="skeptical" size={130} />
        <h1 className="mt-6 text-lg font-medium">Analysis failed</h1>
        <p className="mt-2 text-sm text-gray-600">{error}</p>
        <button
          type="button"
          onClick={() => {
            reset()
            navigate('/app')
          }}
          className="mt-6 rounded-lg bg-black px-4 py-2 text-sm font-medium text-white"
        >
          Try again
        </button>
      </div>
    )
  }

  return (
    <div className="mx-auto flex max-w-md flex-col items-center px-4 pt-16 text-center">
      <Frowns mood={mood} size={130} />
      <h1 className="mt-6 text-lg font-medium">{copy.label}</h1>
      <p className="mt-1 text-sm text-gray-500">{copy.subtitle}</p>

      <div className="mt-6 h-0.5 w-full max-w-xs overflow-hidden rounded-full bg-gray-200">
        <div className={complete ? 'progress-fill progress-fill-done h-full bg-black' : 'progress-fill h-full bg-black'} />
      </div>

      <ol className="mt-5 w-full max-w-xs space-y-2 text-left">
        {STEPS.map((step, index) => (
          <StepRow key={step.label} label={step.label} state={stepState(index, elapsed, complete)} />
        ))}
      </ol>
    </div>
  )
}

function StepRow({ label, state }: { label: string; state: StepState }): ReactElement {
  return (
    <li className="flex items-center gap-2 text-sm">
      <StepIcon state={state} />
      <span
        className={
          state === 'done'
            ? 'text-green-700'
            : state === 'active'
              ? 'font-medium text-gray-900'
              : 'text-gray-400'
        }
      >
        {label}
      </span>
    </li>
  )
}

function StepIcon({ state }: { state: StepState }): ReactElement {
  if (state === 'done') {
    return (
      <span className="flex h-4 w-4 items-center justify-center text-green-600" aria-hidden="true">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
          <path d="M3.5 8.2 6.4 11 12.5 4.8" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
    )
  }

  if (state === 'active') {
    return <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-black" aria-hidden="true" />
  }

  return <span className="h-2.5 w-2.5 rounded-full border border-gray-300" aria-hidden="true" />
}
