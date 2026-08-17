/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    const backendUrl = process.env.BACKEND_URL || 'http://localhost:5889'
    return [
      {
        source: '/api/:path*',
        destination: `${backendUrl}/api/:path*`,
      },
      {
        source: '/assets/:path*',
        destination: `${backendUrl}/assets/:path*`,
      },
    ]
  },
}

export default nextConfig
