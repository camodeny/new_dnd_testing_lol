export default function NumberInput({ id, value, onChange, placeholder, required = false, min, max, className = '' }) {
  return (
    <input
      id={id}
      type="number"
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))}
      placeholder={placeholder}
      required={required}
      min={min}
      max={max}
      className={`input input-number ${className}`}
    />
  )
}
