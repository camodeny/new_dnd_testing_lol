import Link from 'next/link'

export default function NotFound() {
  return (
    <div className="page not-found-page">
      <h2>404</h2>
      <p>This page doesn&apos;t exist or you don&apos;t have access.</p>
      <Link href="/" className="btn btn-secondary">
        <i className="bi bi-arrow-left" aria-hidden="true" /> Back to campaigns
      </Link>
    </div>
  )
}
