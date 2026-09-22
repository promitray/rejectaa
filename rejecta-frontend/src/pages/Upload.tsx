import type { FormEvent, ReactElement } from 'react'
import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { submitPaper } from '../api'
import Frowns from '../components/Frowns'
import { useRejectaStore } from '../store'
import type { Mode } from '../types'

const MAX_FILE_BYTES = 10_485_760

function isPdf(file: File): boolean {
  return file.name.toLowerCase().endsWith('.pdf')
}

export default function Upload(): ReactElement {
  const navigate = useNavigate()
  const mode = useRejectaStore((state) => state.mode)
  const setMode = useRejectaStore((state) => state.setMode)
  const loading = useRejectaStore((state) => state.loading)
  const setLoading = useRejectaStore((state) => state.setLoading)
  const setError = useRejectaStore((state) => state.setError)
  const setResult = useRejectaStore((state) => state.setResult)
  const reset = useRejectaStore((state) => state.reset)

  const inputRef = useRef<HTMLInputElement>(null)
  const submitting = useRef(false)

  const [file, setFile] = useState<File | null>(null)
  const [journal, setJournal] = useState('')
  const [email, setEmail] = useState('')
  const [dragging, setDragging] = useState(false)
  const [fileError, setFileError] = useState<string | null>(null)
  const [journalError, setJournalError] = useState<string | null>(null)
  const [emailError, setEmailError] = useState<string | null>(null)

  function chooseFile(next: File): void {
    if (!isPdf(next)) {
      setFile(null)
      setFileError('PDF files only')
      return
    }
    if (next.size > MAX_FILE_BYTES) {
      setFile(null)
      setFileError('File too large — maximum 10MB')
      return
    }
    setFile(next)
    setFileError(null)
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    if (submitting.current || loading) {
      return
    }

    const nextFileError = !file
      ? 'Upload a PDF to continue'
      : !isPdf(file)
        ? 'PDF files only'
        : file.size > MAX_FILE_BYTES
          ? 'File too large — maximum 10MB'
          : null
    const nextJournalError = journal.trim() ? null : 'Journal name is required'
    const nextEmailError = email.trim() ? null : 'Email is required'

    setFileError(nextFileError)
    setJournalError(nextJournalError)
    setEmailError(nextEmailError)

    if (nextFileError || nextJournalError || nextEmailError || !file) {
      return
    }

    const selected = file
    const selectedMode: Mode = mode
    submitting.current = true

    const currentMode = useRejectaStore.getState().mode
    reset()
    setMode(currentMode)
    setError(null)
    setLoading(true)

    const request = submitPaper(selected, journal.trim(), selectedMode, email.trim())
    navigate('/processing')

    request
      .then((data) => {
        setResult(data)
        setLoading(false)
        navigate('/results')
      })
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : 'Analysis failed — please try again'
        setError(message)
        setLoading(false)
      })
      .finally(() => {
        submitting.current = false
      })
  }

  const dropClass = fileError
    ? 'border-red-400 bg-red-50'
    : file
      ? 'border-green-500 bg-green-50'
      : dragging
        ? 'border-gray-900 bg-gray-50'
        : 'border-gray-300 bg-white'

  return (
    <div className="px-4 py-12">
      <div className="mx-auto flex max-w-md flex-col items-center">
        <Frowns mood="idle" size={100} />
        <h1 className="mt-5 text-center text-2xl font-semibold tracking-tight">
          The desk rejection you never got.
        </h1>
        <p className="mt-2 text-center text-sm leading-6 text-gray-600">
          Upload your manuscript and Professor Frowns will tell you exactly what an editor would reject it for.
        </p>
      </div>

      <form
        onSubmit={handleSubmit}
        className="mx-auto mt-8 max-w-md rounded-xl border border-gray-200 bg-white p-6 shadow-sm"
      >
        <div className="grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={() => setMode('a')}
            className={`rounded-full px-3 py-2 text-sm ${
              mode === 'a' ? 'bg-black text-white' : 'border border-gray-300 bg-white text-gray-600'
            }`}
          >
            My paper
          </button>
          <button
            type="button"
            onClick={() => setMode('b')}
            className={`rounded-full px-3 py-2 text-sm ${
              mode === 'b' ? 'bg-black text-white' : 'border border-gray-300 bg-white text-gray-600'
            }`}
          >
            Someone else&apos;s paper
          </button>
        </div>

        <div
          role="button"
          tabIndex={0}
          onClick={() => inputRef.current?.click()}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault()
              inputRef.current?.click()
            }
          }}
          onDragOver={(event) => {
            event.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault()
            setDragging(false)
            const dropped = event.dataTransfer.files[0]
            if (dropped) {
              chooseFile(dropped)
            }
          }}
          className={`mt-5 cursor-pointer rounded-lg border border-dashed p-8 text-center ${dropClass}`}
        >
          <input
            ref={inputRef}
            type="file"
            accept=".pdf"
            className="hidden"
            onChange={(event) => {
              const chosen = event.target.files?.[0]
              if (chosen) {
                chooseFile(chosen)
              }
            }}
          />
          {file ? (
            <div className="flex flex-col items-center gap-2">
              <span className="flex h-8 w-8 items-center justify-center rounded-full bg-green-600 text-sm text-white">
                ✓
              </span>
              <p className="text-sm font-medium text-gray-900">{file.name}</p>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-2">
              <UploadIcon />
              <p className="text-sm text-gray-800">Drop your PDF here or click to upload</p>
              <p className="text-xs text-gray-500">PDF only · max 10 MB</p>
            </div>
          )}
        </div>
        {fileError ? <p className="mt-2 text-sm text-red-600">{fileError}</p> : null}

        <label className="mt-5 block text-sm font-medium text-gray-800" htmlFor="journal">
          Target journal
        </label>
        <input
          id="journal"
          value={journal}
          onChange={(event) => {
            setJournal(event.target.value)
            setJournalError(null)
          }}
          placeholder="e.g. Nature Machine Intelligence"
          className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-gray-900"
        />
        {journalError ? <p className="mt-2 text-sm text-red-600">{journalError}</p> : null}

        <label className="mt-5 block text-sm font-medium text-gray-800" htmlFor="email">
          Email for report
        </label>
        <input
          id="email"
          type="email"
          value={email}
          onChange={(event) => {
            setEmail(event.target.value)
            setEmailError(null)
          }}
          placeholder="you@university.edu"
          className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-gray-900"
        />
        {emailError ? <p className="mt-2 text-sm text-red-600">{emailError}</p> : null}
        <p className="mt-2 text-xs text-gray-500">Only used to send your report. Never shared.</p>

        <button
          type="submit"
          disabled={loading}
          className="mt-5 flex w-full items-center justify-center gap-2 rounded-lg bg-black px-4 py-2.5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-70"
        >
          {loading ? <Spinner /> : null}
          Analyse my paper →
        </button>
      </form>

      <p id="how-it-works" className="mt-4 text-center text-xs text-gray-500">
        Citation Autopsy is free · full report €19
      </p>
    </div>
  )
}

function UploadIcon(): ReactElement {
  return (
    <svg width="28" height="28" viewBox="0 0 24 24" fill="none" className="text-gray-500" aria-hidden="true">
      <path d="M12 16V5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <path d="M8 8.5 12 4.5 16 8.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M5 16.5V18a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 18v-1.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}

function Spinner(): ReactElement {
  return (
    <span
      className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white"
      aria-hidden="true"
    />
  )
}
