import { Link } from 'react-router-dom'

export default function NotFoundPage() {
  return (
    <div className="page not-found-page">
      <h2>404 - Page Not Found</h2>
      <p>The page you are looking for does not exist.</p>
      <Link to="/">Go Home</Link>
    </div>
  )
}
