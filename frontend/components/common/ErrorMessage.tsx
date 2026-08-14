import type { HTMLAttributes } from 'react'

interface ErrorMessageProps extends HTMLAttributes<HTMLDivElement> {
  message?: string | null
}

export default function ErrorMessage({ message, className = '', ...props }: ErrorMessageProps) {
  if (!message) return null
  return (
    <div className={`error-message ${className}`.trim()} role="alert" {...props}>
      {message}
    </div>
  )
}
