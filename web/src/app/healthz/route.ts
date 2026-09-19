/**
 * Liveness probe for the container runtime.
 *
 * Every page this panel serves needs a session, so Compose needs one path that
 * answers without one. This route deliberately does not touch the app: it
 * reports that the panel process itself is serving requests, which is the only
 * thing a container health check can honestly decide from inside the container.
 */
export function GET() {
  return Response.json({ status: 'ok' })
}
