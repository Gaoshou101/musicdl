/** @type {import('next').NextConfig} */
// The console ships as files, not as a process.
//
// Every page here is a client component and nothing the server would do is
// dynamic, so the build can emit the whole console as static assets. The
// product's own service then serves them beside the API they call, which is
// what keeps a deployment to the app and the plugin runner: one origin, one
// port, no Node runtime in production, and no forwarding hop whose paths could
// drift from the API's.
//
// `trailingSlash` is what makes that work for a deep link. It writes
// `dashboard/config/index.html` instead of `dashboard/config.html`, so a plain
// file server can answer `/dashboard/config/` without being told about routes.
const nextConfig = {
  output: 'export',
  trailingSlash: true,
}

export default nextConfig
