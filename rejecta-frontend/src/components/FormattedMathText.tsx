import type { ReactElement } from 'react'
import katex from 'katex'

interface FormattedMathTextProps {
  text: string
  className?: string
}

/**
 * Parses inline math ($...$) and display math ($$...$$) and renders with KaTeX.
 * Standard text is returned as normal spans.
 */
export default function FormattedMathText({ text, className = '' }: FormattedMathTextProps): ReactElement {
  // Regex to split by $$...$$ (display math) or $...$ (inline math)
  const mathRegex = /(\$\$[\s\S]*?\$\$|\$[^\$]+?\$)/g

  const parts = text.split(mathRegex)

  return (
    <span className={className}>
      {parts.map((part, index) => {
        if (!part) return null

        if (part.startsWith('$$') && part.endsWith('$$') && part.length > 4) {
          const math = part.slice(2, -2).trim()
          try {
            const html = katex.renderToString(math, {
              displayMode: true,
              throwOnError: false,
            })
            return (
              <span
                key={index}
                className="my-2 block overflow-x-auto py-1"
                dangerouslySetInnerHTML={{ __html: html }}
              />
            )
          } catch {
            return <code key={index} className="text-stone-700">{part}</code>
          }
        }

        if (part.startsWith('$') && part.endsWith('$') && part.length > 2) {
          const math = part.slice(1, -1).trim()
          try {
            const html = katex.renderToString(math, {
              displayMode: false,
              throwOnError: false,
            })
            return (
              <span
                key={index}
                className="inline-block px-0.5 align-baseline"
                dangerouslySetInnerHTML={{ __html: html }}
              />
            )
          } catch {
            return <code key={index} className="text-stone-700">{part}</code>
          }
        }

        return <span key={index}>{part}</span>
      })}
    </span>
  )
}
