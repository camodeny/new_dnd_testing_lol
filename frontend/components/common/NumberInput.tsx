import { forwardRef, type InputHTMLAttributes } from 'react'

interface NumberInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'value' | 'onChange'> {
  value: number | null | undefined
  onChange: (value: number | null) => void
}

const NumberInput = forwardRef<HTMLInputElement, NumberInputProps>(
  ({ className = '', value, onChange, ...props }, ref) => (
    <input
      ref={ref}
      type="number"
      className={`input ${className}`.trim()}
      value={value ?? ''}
      onChange={(event) => onChange(event.target.value === '' ? null : event.target.valueAsNumber)}
      {...props}
    />
  ),
)

NumberInput.displayName = 'NumberInput'
export default NumberInput
