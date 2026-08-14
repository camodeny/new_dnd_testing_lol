import { HTMLAttributes } from 'react'

interface FormGroupProps extends HTMLAttributes<HTMLDivElement> {
  label: string
  htmlFor?: string
  hint?: string
  error?: string
}

export default function FormGroup({
  label,
  htmlFor,
  hint,
  error,
  className = '',
  children,
  ...props
}: FormGroupProps) {
  return (
    <div className={`form-group ${className}`.trim()} {...props}>
      <label htmlFor={htmlFor} style={{ display: 'block', marginBottom: 6 }}>
        {label}
      </label>
      {children}
      {hint && !error && (
        <p style={{ margin: '5px 0 0', color: 'var(--ink-faint)', fontSize: '0.72rem' }}>{hint}</p>
      )}
      {error && (
        <p style={{ margin: '5px 0 0', color: 'var(--danger)', fontSize: '0.72rem' }}>{error}</p>
      )}
    </div>
  )
}
