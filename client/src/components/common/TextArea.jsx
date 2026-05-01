export default function TextArea({ id, value, onChange, placeholder, rows = 3, className = '' }) {
  return (
    <textarea
      id={id}
      value={value}
      onChange={onChange}
      placeholder={placeholder}
      rows={rows}
      className={`textarea ${className}`}
    />
  )
}
