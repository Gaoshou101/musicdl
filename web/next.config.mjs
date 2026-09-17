/** @type {import('next').NextConfig} */
// The browser only ever calls `/api/*` on this server; the rewrite below is
// what turns that into the app's own `/admin/*` routes. Naming the app in one
// place keeps a deployment from having to guess, and reading it from the
// environment keeps the same source usable against a local app, an SSH tunnel,
// or the Compose service that runs the app.
//
// Next resolves rewrites while it builds, so this origin ends up fixed in the
// built server: set it for the build (`MUSICDL_API_ORIGIN=... next build`), not
// for the process that later runs it.
const apiOrigin = (process.env.MUSICDL_API_ORIGIN || 'http://127.0.0.1:8000').replace(/\/+$/, '')

const nextConfig = {
  output: 'standalone',
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${apiOrigin}/admin/:path*`,
      },
    ]
  },
}

export default nextConfig
