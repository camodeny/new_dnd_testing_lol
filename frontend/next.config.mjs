/** @type {import('next').NextConfig} */
const backendUrl = process.env.BACKEND_URL
const rewriteBackendUrl = backendUrl || 'http://localhost:5889'

const nextConfig = {
  // Character chat streams directly to the backend to avoid proxy buffering.
  // Keep that browser-visible target derived from the same setting as rewrites.
  // Only expose an explicitly configured URL to browsers. The local rewrite
  // fallback must not send production browsers to their own localhost.
  env: backendUrl ? { BACKEND_URL: backendUrl } : {},
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${rewriteBackendUrl}/api/:path*`,
      },
      {
        source: '/assets/:path*',
        destination: `${rewriteBackendUrl}/assets/:path*`,
      },
    ]
  },
}

export default nextConfig
