import type { ReactElement } from 'react'
import type { Mood } from '../types'

interface FrownsProps {
  mood: Mood | 'idle'
  size?: number
}

interface FacePaths {
  leftBrow: string
  rightBrow: string
  mouth: string
}

const FACES: Record<Mood | 'idle', FacePaths> = {
  furious: {
    leftBrow: 'M 54 58 Q 70 54 90 76',
    rightBrow: 'M 146 58 Q 130 54 110 76',
    mouth: 'M 70 128 Q 100 98 130 128',
  },
  skeptical: {
    leftBrow: 'M 56 64 Q 72 60 90 72',
    rightBrow: 'M 144 64 Q 128 60 110 72',
    mouth: 'M 74 120 Q 100 108 126 120',
  },
  unimpressed: {
    leftBrow: 'M 56 66 Q 74 66 90 66',
    rightBrow: 'M 110 66 Q 126 66 144 66',
    mouth: 'M 74 118 Q 100 118 126 118',
  },
  neutral: {
    leftBrow: 'M 56 68 Q 74 62 90 68',
    rightBrow: 'M 110 68 Q 126 62 144 68',
    mouth: 'M 74 116 Q 100 124 126 116',
  },
  interested: {
    leftBrow: 'M 56 70 Q 74 58 92 66',
    rightBrow: 'M 108 66 Q 126 58 144 70',
    mouth: 'M 72 114 Q 100 130 128 114',
  },
  delighted: {
    leftBrow: 'M 52 72 Q 74 50 94 64',
    rightBrow: 'M 106 64 Q 126 50 148 72',
    mouth: 'M 68 112 Q 100 140 132 112',
  },
  idle: {
    leftBrow: 'M 56 68 Q 74 62 90 68',
    rightBrow: 'M 110 68 Q 126 62 144 68',
    mouth: 'M 74 116 Q 100 124 126 116',
  },
}

export default function Frowns({ mood, size = 120 }: FrownsProps): ReactElement {
  const face = FACES[mood]
  const penClass = mood === 'idle' ? 'frowns-pen frowns-pen-idle' : 'frowns-pen'

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 200 200"
      role="img"
      aria-label={`Professor Frowns, ${mood}`}
      className="text-gray-800"
    >
      <circle
        cx="100"
        cy="86"
        r="58"
        className="fill-gray-50 stroke-gray-200"
        strokeWidth="3"
      />

      <ellipse cx="78" cy="86" rx="16" ry="13" fill="none" stroke="currentColor" strokeWidth="2.5" />
      <ellipse cx="122" cy="86" rx="16" ry="13" fill="none" stroke="currentColor" strokeWidth="2.5" />
      <path d="M 94 86 H 106" fill="none" stroke="currentColor" strokeWidth="2.5" />
      <path d="M 62 84 L 50 78" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
      <path d="M 138 84 L 150 78" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />

      <circle cx="78" cy="87" r="3.2" fill="currentColor" />
      <circle cx="122" cy="87" r="3.2" fill="currentColor" />

      <path
        d={face.leftBrow}
        className="frowns-feature"
        fill="none"
        stroke="currentColor"
        strokeWidth="3.5"
        strokeLinecap="round"
      />
      <path
        d={face.rightBrow}
        className="frowns-feature"
        fill="none"
        stroke="currentColor"
        strokeWidth="3.5"
        strokeLinecap="round"
      />
      <path
        d={face.mouth}
        className="frowns-feature"
        fill="none"
        stroke="currentColor"
        strokeWidth="3.5"
        strokeLinecap="round"
      />

      <g>
        <rect
          x="16"
          y="150"
          width="36"
          height="42"
          rx="3"
          className="fill-gray-50"
          stroke="currentColor"
          strokeWidth="2"
        />
        <path d="M 22 162 H 46 M 22 170 H 46 M 22 178 H 40" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </g>

      <g className={penClass}>
        <path
          d="M 168 168 L 188 148"
          fill="none"
          stroke="currentColor"
          strokeWidth="3.5"
          strokeLinecap="round"
        />
        <path d="M 164 172 L 172 164 L 168 176 Z" fill="currentColor" />
        <path d="M 186 146 L 192 152" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
      </g>
    </svg>
  )
}
