interface LoadingProps {
  message?: string
  className?: string
}

export default function Loading({ message = 'Loading…', className = '' }: LoadingProps) {
  return (
    <div className={`loading ${className}`.trim()} role="status" aria-live="polite">
      <span>{message}</span>
    </div>
  )
}
