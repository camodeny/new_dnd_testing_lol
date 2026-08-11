export default function Input({ id, type = 'text', value, onChange, placeholder, required = false, min, max, className = '' }) {
  return (
    <input
      id={id}
      type={type}
      value={value}
      onChange={onChange}
      placeholder={placeholder}
      required={required}
      min={min}
      max={max}
      className={`input ${className}`}
    />
  )
}
