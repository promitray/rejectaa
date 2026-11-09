import type { FormEvent, ReactElement } from 'react'
import { lazy, Suspense, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { submitPaper } from '../api'
import { normaliseOrcid } from '../authors/ids'
import { useAuthorProfiles } from '../authors/useAuthorProfiles'
import AuthorsEditor from '../components/AuthorsEditor'
import Frowns from '../components/Frowns'
import { useRejectaStore } from '../store'
import type { ArticleType, Mode } from '../types'

// Charts pull in Recharts, so they load only once an author has been entered.
const AuthorInsights = lazy(() => import('../components/AuthorInsights'))

const MAX_FILE_BYTES = 10_485_760
const JOURNAL_SUGGESTIONS = [
  'Nature',
  'PLOS ONE',
  'Nature Machine Intelligence',
  'The Lancet',
]

const ACCEPTED_EXTENSIONS = ['.pdf', '.docx']
const FORMAT_ERROR = 'Upload a PDF or a Word (.docx) file'
const LEGACY_DOC_ERROR = 'Old .doc files are not supported. In Word use Save As .docx and upload that'

function isAccepted(file: File): boolean {
  const name = file.name.toLowerCase()
  return ACCEPTED_EXTENSIONS.some((extension) => name.endsWith(extension))
}

function formatError(file: File): string {
  return file.name.toLowerCase().endsWith('.doc') ? LEGACY_DOC_ERROR : FORMAT_ERROR
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
  const authors = useRejectaStore((state) => state.authors)
  const setAuthors = useRejectaStore((state) => state.setAuthors)

  const { states: authorStates, busy: authorsBusy, lookup: lookupAuthors } = useAuthorProfiles()
  const [authorsError, setAuthorsError] = useState<string | null>(null)
  // The record appears after "Analyse authors" has been pressed at least once.
  const showInsights = Object.keys(authorStates).length > 0

  function analyseAuthors(): void {
    const valid = authors.flatMap((author) => normaliseOrcid(author.orcid) ?? [])
    if (valid.length === 0) {
      setAuthorsError('Enter at least one valid ORCID to analyse.')
      return
    }
    setAuthorsError(null)
    lookupAuthors(valid)
  }

  const inputRef = useRef<HTMLInputElement>(null)
  const submitting = useRef(false)

  const [file, setFile] = useState<File | null>(null)
  const [journal, setJournal] = useState('')
  const [articleType, setArticleType] = useState<ArticleType | 'auto'>('auto')
  const [keepCopy, setKeepCopy] = useState(true)
  const [email, setEmail] = useState('')
  const [dragging, setDragging] = useState(false)
  const [fileError, setFileError] = useState<string | null>(null)
  const [journalError, setJournalError] = useState<string | null>(null)
  const [emailError, setEmailError] = useState<string | null>(null)
  const [sampleLoading, setSampleLoading] = useState(false)

  function chooseFile(next: File): void {
    if (!isAccepted(next)) {
      setFile(null)
      setFileError(formatError(next))
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

  async function loadSample(): Promise<void> {
    setSampleLoading(true)
    setFileError(null)
    try {
      const response = await fetch('/sample_readmission.pdf')
      if (!response.ok) {
        throw new Error('Could not load the sample manuscript')
      }
      const blob = await response.blob()
      chooseFile(new File([blob], 'sample_readmission.pdf', { type: 'application/pdf' }))
      setJournal('PLOS ONE')
      setJournalError(null)
    } catch {
      setFileError('Could not load the sample manuscript')
    } finally {
      setSampleLoading(false)
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    if (submitting.current || loading) {
      return
    }

    const nextFileError = !file
      ? 'Upload a PDF or Word file to continue'
      : !isAccepted(file)
        ? formatError(file)
        : file.size > MAX_FILE_BYTES
          ? 'File too large — maximum 10MB'
          : null
    const nextJournalError = journal.trim() ? null : 'Journal name is required'
    const nextEmailError = email.trim() && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())
      ? 'Enter a valid email, or leave it blank'
      : null

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

    const request = submitPaper(selected, journal.trim(), selectedMode, email.trim(), articleType, keepCopy)
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

      <div className="mx-auto mt-8 grid max-w-md gap-6 lg:max-w-5xl lg:grid-cols-[minmax(0,28rem)_minmax(0,1fr)] lg:items-start">
      <form
        onSubmit={handleSubmit}
        className="w-full rounded-xl border border-gray-200 bg-white p-6 shadow-sm"
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
            accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
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
              <p className="text-sm text-gray-800">Drop your paper here or click to upload</p>
              <p className="text-xs text-gray-500">PDF or Word (.docx) · max 10 MB</p>
            </div>
          )}
        </div>
        {fileError ? <p className="mt-2 text-sm text-red-600">{fileError}</p> : null}
        <button
          type="button"
          onClick={() => {
            void loadSample()
          }}
          disabled={sampleLoading}
          className="mt-3 w-full text-center text-xs text-gray-600 underline-offset-2 hover:underline disabled:opacity-60"
        >
          {sampleLoading ? 'Loading sample…' : 'Use the sample manuscript instead'}
        </button>

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
        <div className="mt-2 flex flex-wrap gap-1.5">
          {JOURNAL_SUGGESTIONS.map((name) => (
            <button
              key={name}
              type="button"
              onClick={() => {
                setJournal(name)
                setJournalError(null)
              }}
              className={`rounded-full border px-2.5 py-1 text-xs ${
                journal === name
                  ? 'border-gray-900 bg-gray-900 text-white'
                  : 'border-gray-300 bg-white text-gray-600 hover:border-gray-500'
              }`}
            >
              {name}
            </button>
          ))}
        </div>
        {journalError ? <p className="mt-2 text-sm text-red-600">{journalError}</p> : null}

        <label className="mt-5 block text-sm font-medium text-gray-800" htmlFor="article-type">
          Article type <span className="font-normal text-gray-500">(optional)</span>
        </label>
        <select
          id="article-type"
          value={articleType}
          onChange={(event) => setArticleType(event.target.value as ArticleType | 'auto')}
          className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm outline-none focus:border-gray-900"
        >
          <option value="auto">Detect automatically</option>
          <option value="research_article">Full paper / research article</option>
          <option value="communication">Communication / letter</option>
          <option value="perspective">Perspective</option>
          <option value="review">Review article</option>
          <option value="mini_review">Mini-review</option>
        </select>
        <p className="mt-1 text-xs text-gray-500">
          The type decides which sections are expected, so a short communication is not marked down for sections it does not have.
        </p>

        <label className="mt-5 flex items-start gap-2 text-sm text-gray-800">
          <input
            type="checkbox"
            checked={keepCopy}
            onChange={(event) => setKeepCopy(event.target.checked)}
            className="mt-0.5"
          />
          <span>
            Keep an editable copy for 24 hours
            <span className="block text-xs text-gray-500">
              Lets you apply calibrated sections to your paper and download it as Word. The copy is stored on our
              server and deleted after 24 hours, or earlier if you delete it. Without this, nothing is stored.
            </span>
          </span>
        </label>

        <label className="mt-5 block text-sm font-medium text-gray-800" htmlFor="email">
          Email <span className="font-normal text-gray-500">(optional)</span>
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
        <p className="mt-2 text-xs text-gray-500">
          Not stored. Leave blank — the report stays on this page.
        </p>

        <button
          type="submit"
          disabled={loading}
          className="mt-5 flex w-full items-center justify-center gap-2 rounded-lg bg-black px-4 py-2.5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-70"
        >
          {loading ? <Spinner /> : null}
          Analyse my paper →
        </button>
      </form>

      <div className="min-w-0 space-y-6">
        <AuthorsEditor
          authors={authors}
          onChange={setAuthors}
          states={authorStates}
          onRetry={(orcid) => lookupAuthors([orcid])}
          onAnalyse={analyseAuthors}
          busy={authorsBusy}
          error={authorsError}
        />
        {showInsights ? (
          <Suspense
            fallback={
              <div className="rounded-xl border border-gray-200 bg-white p-5 text-sm text-gray-500 shadow-sm">
                Loading author record…
              </div>
            }
          >
            <AuthorInsights authors={authors} states={authorStates} />
          </Suspense>
        ) : null}
      </div>
      </div>

      <p className="mt-4 text-center text-xs text-gray-500">
        Research preview · full report included · uses one paper analysis from your plan
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
