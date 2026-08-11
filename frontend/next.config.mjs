/** @type {import('next').NextConfig} */
const nextConfig = {
  output: process.env.VERCEL ? undefined : "standalone",
  async rewrites() {
    // When running locally, proxy /api/* to the FastAPI backend.
    // On Vercel this is unset and vercel.json's service rewrite handles routing.
    const backendUrl = process.env.BACKEND_URL;
    if (!backendUrl) {
      return [];
    }
    return [
      {
        source: "/api/:path*",
        destination: `${backendUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
