export default function Button({ children, type = 'button', variant = 'primary', disabled = false, onClick, className = '' }) {
  const base = 'btn'
  const variantClass = `btn-${variant}`
  return (
    <button
      type={type}
      className={`${base} ${variantClass} ${className}`}
      disabled={disabled}
      onClick={onClick}
    >
      {children}
    </button>
  )
}
