// Persisted invite continuation fallback — issue #242.
//
// Shareable /invite/:code links park unauthenticated recipients in
// localStorage (plus ?next=) so sign-up/sign-in — including Supabase
// email-confirmation round-trips that may drop the path — can always
// resume the intended invite. The stored value is only ever a code the
// backend must still validate on lookup/accept; it grants nothing.

export const PENDING_INVITE_KEY = 'pendingInviteCode'

export function isValidInviteCodeShape(code: string | null | undefined): boolean {
  return typeof code === 'string' && /^[A-Z0-9]{4,20}$/.test(code)
}

function safeNextPath(raw: string | null): string | null {
  if (!raw) return null
  // Only same-origin absolute paths continue — never open-redirect.
  if (!raw.startsWith('/') || raw.startsWith('//')) return null
  return raw
}

// Invite continuation derived from the rendered location (#242): AppShell
// renders LoginPage directly at /invite/:code for signed-out recipients,
// so there is no ?next= in that path — the pathname itself is the context.
export function inviteContinuation(): { next: string; code: string } | null {
  if (typeof window === 'undefined') return null
  const fromParam = safeNextPath(new URLSearchParams(window.location.search).get('next'))
  if (fromParam) {
    const match = /^\/invite\/([A-Za-z0-9_-]{1,20})\/?$/.exec(fromParam)
    return { next: fromParam, code: (match?.[1] ?? '').toUpperCase() || '' }
  }
  const match = /^\/invite\/([A-Za-z0-9_-]{1,20})\/?$/.exec(window.location.pathname)
  if (!match) return null
  return { next: `/invite/${match[1].toUpperCase()}`, code: match[1].toUpperCase() }
}

export function invitePathFor(code: string): string {
  return `/invite/${code}`
}

export function storePendingInvite(code: string): void {
  const clean = (code ?? '').toUpperCase()
  if (!isValidInviteCodeShape(clean)) return
  try {
    localStorage.setItem(PENDING_INVITE_KEY, clean)
  } catch { /* no-op */ }
}

export function readPendingInvite(): string | null {
  try {
    const raw = localStorage.getItem(PENDING_INVITE_KEY)
    return isValidInviteCodeShape(raw) ? (raw as string) : null
  } catch {
    return null
  }
}

export function clearPendingInvite(code?: string): void {
  try {
    if (code === undefined || localStorage.getItem(PENDING_INVITE_KEY) === code) {
      localStorage.removeItem(PENDING_INVITE_KEY)
    }
  } catch { /* no-op */ }
}

// Where an authenticated landing should continue. Returns the invite path
// when a valid pending code exists and the app is NOT already on it;
// clears abandoned/malformed values deliberately so a stale code can never
// trap the user in a recovery loop. Returns null when there is nothing to
// recover (or the invite page itself owns the flow from here).
export function recoverPendingInviteTarget(currentPath: string): string | null {
  let raw: string | null = null
  try {
    raw = localStorage.getItem(PENDING_INVITE_KEY)
  } catch {
    return null
  }
  if (raw === null) return null
  if (!isValidInviteCodeShape(raw)) {
    try {
      localStorage.removeItem(PENDING_INVITE_KEY)
    } catch { /* no-op */ }
    return null
  }
  if (currentPath === invitePathFor(raw)) return null
  return invitePathFor(raw)
}
