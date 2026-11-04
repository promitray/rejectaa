import type { ReactElement } from 'react'

/** A limit: a non-negative number, or "Unlimited" (null). */
export default function LimitInput({
  value,
  onChange,
  label,
  disabled = false,
}: {
  value: number | null
  onChange: (value: number | null) => void
  label: string
  disabled?: boolean
}): ReactElement {
  const unlimited = value === null
  return (
    <div className="flex items-center gap-2">
      <input
        type="number"
        min={0}
        max={1000000}
        step={1}
        aria-label={label}
        disabled={disabled || unlimited}
        value={unlimited ? '' : value}
        placeholder={unlimited ? '∞' : '0'}
        onChange={(e) => {
          const n = e.target.value === '' ? 0 : Math.max(0, Math.floor(Number(e.target.value)))
          onChange(Number.isFinite(n) ? n : 0)
        }}
        className="w-24 rounded-lg border border-gray-300 px-2 py-1.5 text-sm outline-none focus:border-gray-900 disabled:bg-gray-50"
      />
      <label className="flex items-center gap-1.5 whitespace-nowrap text-xs text-gray-600">
        <input
          type="checkbox"
          checked={unlimited}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked ? null : 0)}
        />
        Unlimited
      </label>
    </div>
  )
}
