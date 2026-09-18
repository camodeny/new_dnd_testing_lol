// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest'
import {
  PENDING_INVITE_KEY,
  clearPendingInvite,
  recoverPendingInviteTarget,
  storePendingInvite,
} from './pendingInvite'

describe('pending-invite recovery (#242)', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('recovers the stored invite when auth lands away from /invite/:code', () => {
    storePendingInvite('TESTCODE8')
    // Email-confirmation redirect dropped the path and landed at /.
    expect(recoverPendingInviteTarget('/')).toBe('/invite/TESTCODE8')
  })

  it('recovery is one-shot: a dead invite never hijacks navigation twice', () => {
    storePendingInvite('TESTCODE8')
    expect(recoverPendingInviteTarget('/')).toBe('/invite/TESTCODE8')
    // Consumed on redirect — a second landing at / stays put instead of
    // bouncing back to a revoked/expired/full/started/unknown invite.
    expect(localStorage.getItem(PENDING_INVITE_KEY)).toBeNull()
    expect(recoverPendingInviteTarget('/')).toBeNull()
  })

  it('is a no-op without a stored code so ordinary logins are untouched', () => {
    expect(recoverPendingInviteTarget('/')).toBeNull()
  })

  it('is a no-op on the invite path itself (invite page owns the flow)', () => {
    storePendingInvite('TESTCODE8')
    expect(recoverPendingInviteTarget('/invite/TESTCODE8')).toBeNull()
    // Code stays parked until acceptance clears it.
    expect(localStorage.getItem(PENDING_INVITE_KEY)).toBe('TESTCODE8')
  })

  it('clears malformed values instead of trapping the user in a loop', () => {
    localStorage.setItem(PENDING_INVITE_KEY, 'not a code!!')
    expect(recoverPendingInviteTarget('/')).toBeNull()
    expect(localStorage.getItem(PENDING_INVITE_KEY)).toBeNull()
  })

  it('clearPendingInvite only clears the matching code', () => {
    storePendingInvite('TESTCODE8')
    clearPendingInvite('OTHERCODE1')
    expect(localStorage.getItem(PENDING_INVITE_KEY)).toBe('TESTCODE8')
    clearPendingInvite('TESTCODE8')
    expect(localStorage.getItem(PENDING_INVITE_KEY)).toBeNull()
  })
})
