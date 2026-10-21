import { forwardRef, useEffect, useRef, useState, type InputHTMLAttributes } from 'react'

interface NumberInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'value' | 'onChange'> {
  value: number | null | undefined
  onChange: (value: number | null) => void
}

const NumberInput = forwardRef<HTMLInputElement, NumberInputProps>(
  ({ className = '', value, onChange, ...props }, ref) => {
    // While focused, keep the raw keystrokes local and only emit numbers
    // that parse. Sections coerce null back to a default, so emitting null
    // on every keystroke (or mirroring the prop while typing) would snap a
    // half-cleared "10" back before the next digit lands.
    const [draft, setDraft] = useState<string | null>(null)
    const focusedRef = useRef(false)

    useEffect(() => {
      if (!focusedRef.current) setDraft(null)
    }, [value])

    const minValue = props.min === undefined || props.min === '' ? undefined : Number(props.min)
    const inputMode = props.inputMode ?? (minValue !== undefined && minValue >= 0 ? 'numeric' : undefined)

    return (
      <input
        {...props}
        ref={ref}
        type="number"
        className={`input ${className}`.trim()}
        value={draft ?? value ?? ''}
        inputMode={inputMode}
        onFocus={(event) => {
          focusedRef.current = true
          props.onFocus?.(event)
        }}
        onChange={(event) => {
          const raw = event.target.value
          setDraft(raw)
          if (raw.trim() === '') {
            onChange(null)
            return
          }
          const parsed = Number(raw)
          if (!Number.isNaN(parsed)) onChange(parsed)
        }}
        onBlur={(event) => {
          // Fall back to the incoming value, letting the parent's default
          // show again when the field was left empty or unparseable.
          focusedRef.current = false
          setDraft(null)
          props.onBlur?.(event)
        }}
      />
    )
  },
)

NumberInput.displayName = 'NumberInput'
export default NumberInput
