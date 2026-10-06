import type { ReactElement } from 'react'
import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import Frowns from '../components/Frowns'
import Footer from '../components/Footer'

const FEATURES = [
  {
    title: 'Citation Autopsy',
    body: 'Every reference is checked against CrossRef, with an arXiv fallback. Verified, ghost, or unverified — before a reviewer finds the ones that do not exist.',
  },
  {
    title: 'Professor Frowns',
    body: 'A first-person editorial letter, section scores, and the reasons a desk editor would stop reading. Written as if it came from the journal, not a grammar tool.',
  },
  {
    title: 'Journal fit',
    body: 'The target venue is looked up on OpenAlex. Recent papers and a fit paragraph tell you whether the manuscript belongs there, or somewhere else.',
  },
]

const STEPS = [
  {
    n: '01',
    title: 'Upload the manuscript',
    body: 'PDF only. Name the target journal. No account. The file is parsed in memory and is not stored.',
  },
  {
    n: '02',
    title: 'References and venue are checked',
    body: 'Citations go to CrossRef. The journal name goes to OpenAlex. Those two run together so you are not waiting twice.',
  },
  {
    n: '03',
    title: 'Read the desk review',
    body: 'Mood, score, letter, section notes, rejection reasons, novelty, and journal fit — in one report you can print.',
  },
]

const REPORT = [
  'Editorial mood and a 0–100 score',
  'The oracle letter — what the editor would write',
  'Section scores with an issue and a fix (or strength and weakness)',
  'Desk-reject / major / minor reasons',
  'Novelty assessment against the venue',
  'Citation table: verified, ghost, unverified',
  'Journal match and recent papers from that source',
]

const FAQ = [
  {
    q: 'Do I need an account?',
    a: 'No. Try it now opens the upload form. Email is optional and is not stored.',
  },
  {
    q: 'Is the manuscript kept?',
    a: 'No. The PDF is read for this request only. There is no file store and no report archive yet.',
  },
  {
    q: 'What files work?',
    a: 'Academic PDFs up to 10 MB. Unusual layouts can miss a heading or a reference. That is a parser limit, not a silent failure.',
  },
  {
    q: 'Is this a peer review?',
    a: 'No. It is a pre-submission desk review: citation integrity, substance, and venue fit. It does not replace colleagues or the journal.',
  },
]

export default function Landing(): ReactElement {
  useEffect(() => {
    const id = window.location.hash.replace('#', '')
    if (!id) {
      return
    }
    window.requestAnimationFrame(() => {
      document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' })
    })
  }, [])

  return (
    <div className="min-h-screen bg-white text-gray-900">
      <header className="border-b border-gray-200">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <a href="#top" className="flex items-center gap-2 text-base font-semibold tracking-tight">
            <span className="h-2 w-2 rounded-full bg-red-600" aria-hidden="true" />
            Rejecta
          </a>
          <nav className="flex items-center gap-4">
            <a href="#how-it-works" className="text-sm text-gray-600 hover:text-gray-900">
              How it works
            </a>
            <Link
              to="/app"
              className="rounded-lg bg-black px-3 py-1.5 text-sm font-medium text-white"
            >
              Try it now
            </Link>
          </nav>
        </div>
      </header>

      <main id="top">
        <section className="mx-auto grid max-w-5xl items-center gap-10 px-4 py-16 sm:grid-cols-[1.2fr_0.8fr] sm:py-24">
          <div>
            <p className="text-xs font-medium uppercase tracking-wider text-gray-500">
              Pre-submission desk review
            </p>
            <h1 className="mt-3 font-serif text-4xl leading-tight tracking-tight sm:text-5xl">
              The desk rejection you never got.
            </h1>
            <p className="mt-5 max-w-xl text-base leading-7 text-gray-600">
              Grammar tools fix sentences. Rejecta works on why a paper never reaches peer review:
              ghost citations, thin sections, and a venue that does not fit. Upload a manuscript.
              Read what an editor would reject it for — before you submit.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-3">
              <Link
                to="/app"
                className="rounded-lg bg-black px-5 py-2.5 text-sm font-medium text-white"
              >
                Try it now
              </Link>
              <a href="#how-it-works" className="text-sm text-gray-700 underline-offset-4 hover:underline">
                See how it works
              </a>
            </div>
            <p className="mt-4 text-xs text-gray-500">
              No account · PDF only · manuscript is not stored
            </p>
          </div>
          <div className="flex flex-col items-center justify-center rounded-2xl border border-gray-200 bg-gray-50 px-6 py-10">
            <Frowns mood="skeptical" size={160} />
            <p className="mt-4 text-center text-sm font-medium">Prof. Aldric Frowns</p>
            <p className="text-center text-xs text-gray-500">DPhil (Cantab) · editorial board, retired from patience</p>
          </div>
        </section>

        <section className="border-y border-gray-200 bg-gray-50">
          <div className="mx-auto grid max-w-5xl gap-8 px-4 py-14 sm:grid-cols-3">
            {FEATURES.map((item) => (
              <article key={item.title}>
                <h2 className="text-sm font-semibold">{item.title}</h2>
                <p className="mt-2 text-sm leading-6 text-gray-600">{item.body}</p>
              </article>
            ))}
          </div>
        </section>

        <section id="how-it-works" className="mx-auto max-w-5xl px-4 py-16">
          <h2 className="font-serif text-3xl tracking-tight">How it works</h2>
          <p className="mt-3 max-w-2xl text-sm leading-6 text-gray-600">
            One request. The API parses the PDF, checks references, looks up the journal, then asks
            the model for a critique. You stay on the page until the letter is ready.
          </p>
          <ol className="mt-10 grid gap-6 sm:grid-cols-3">
            {STEPS.map((step) => (
              <li key={step.n} className="rounded-xl border border-gray-200 bg-white p-5">
                <p className="text-xs font-medium text-gray-400">{step.n}</p>
                <h3 className="mt-2 text-sm font-semibold">{step.title}</h3>
                <p className="mt-2 text-sm leading-6 text-gray-600">{step.body}</p>
              </li>
            ))}
          </ol>
        </section>

        <section className="border-y border-gray-200 bg-gray-50">
          <div className="mx-auto grid max-w-5xl gap-10 px-4 py-16 sm:grid-cols-2">
            <div>
              <h2 className="font-serif text-3xl tracking-tight">What the report includes</h2>
              <p className="mt-3 text-sm leading-6 text-gray-600">
                Two modes. <span className="font-medium text-gray-800">My paper</span> returns an
                issue and a fix per section. <span className="font-medium text-gray-800">Someone
                else&apos;s paper</span> returns strength and weakness. Same pipeline either way.
              </p>
              <ul className="mt-6 space-y-2">
                {REPORT.map((line) => (
                  <li key={line} className="flex gap-2 text-sm text-gray-700">
                    <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-red-600" aria-hidden="true" />
                    {line}
                  </li>
                ))}
              </ul>
            </div>
            <div className="rounded-xl border border-gray-200 bg-white p-6">
              <p className="text-xs font-medium uppercase tracking-wider text-gray-500">For</p>
              <p className="mt-2 text-sm leading-6 text-gray-700">
                Authors about to submit. Supervisors reading a draft. Anyone who has been desk
                rejected for a reason that was sitting in the PDF the whole time.
              </p>
              <p className="mt-6 text-xs font-medium uppercase tracking-wider text-gray-500">Not for</p>
              <p className="mt-2 text-sm leading-6 text-gray-700">
                Hiding AI-drafted prose, or replacing a colleague who knows the field. Rejecta
                does not claim undetectable writing and does not issue a journal decision.
              </p>
              <Link
                to="/app"
                className="mt-8 inline-flex rounded-lg bg-black px-4 py-2.5 text-sm font-medium text-white"
              >
                Open the desk
              </Link>
            </div>
          </div>
        </section>

        <section className="mx-auto max-w-5xl px-4 py-16">
          <h2 className="font-serif text-3xl tracking-tight">Questions</h2>
          <dl className="mt-8 divide-y divide-gray-200 border-y border-gray-200">
            {FAQ.map((item) => (
              <div key={item.q} className="grid gap-2 py-5 sm:grid-cols-[16rem_1fr] sm:gap-8">
                <dt className="text-sm font-medium">{item.q}</dt>
                <dd className="text-sm leading-6 text-gray-600">{item.a}</dd>
              </div>
            ))}
          </dl>
        </section>
      </main>

      <Footer />
    </div>
  )
}
