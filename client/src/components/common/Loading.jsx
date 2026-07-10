export default function Loading({ message = 'Loading...' }) {
  return (
    <div className="loading" role="status" aria-live="polite" aria-atomic="true">
      {message}
    </div>
  )
}
