/*!
 * @name Contract Check Source
 * @version 1.0.0
 * @description The smallest lx-shaped script the admin panel's contract check
 *   can import. It reaches one plain-HTTP endpoint, so importing it always
 *   needs an explicit operator grant -- which is what makes the check's grant
 *   step mean something. Nothing here is ever run: the check only analyses and
 *   stores it.
 */
const { EVENT_NAMES, request, on, send } = globalThis.lx
const API = 'http://music.example.com/api'

on(EVENT_NAMES.request, ({ action, info }) => {
  if (action !== 'musicUrl') return
  return request(`${API}/url?id=${info.musicInfo.id}`, { method: 'GET' })
})

send(EVENT_NAMES.inited, { status: true, sources: { check: { name: 'Check' } } })
