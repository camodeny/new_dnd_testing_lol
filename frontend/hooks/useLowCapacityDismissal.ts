'use client'

/**
 * Low-capacity notice dismissal — a per-device, per-campaign reading aid.
 *
 * Dismissing hides only the quiet "getting low" heads-up. It clears once
 * capacity leaves the low band (restored, grace, or paused), so the next low
 * stretch shows again. Grace and pause notices are never dismissible.
 */

import { useCallback, useEffect, useState } from 'react'
import type { CapacityViewState } from '@/lib/capacity'

function dismissedKey(campaignId: string) {
  return `fireside:capacity-low-dismissed:${campaignId}`
}

export function useLowCapacityDismissal(campaignId: string | null, state: CapacityViewState) {
  const key = campaignId ? dismissedKey(campaignId) : null
  const [dismissed, setDismissed] = useState(false)

  useEffect(() => {
    if (!key) return
    setDismissed(window.localStorage.getItem(key) === '1')
  }, [key])

  // `unavailable` is loading/failure, not a real change in capacity.
  useEffect(() => {
    if (!key || state === 'low' || state === 'unavailable') return
    window.localStorage.removeItem(key)
    setDismissed(false)
  }, [key, state])

  const dismiss = useCallback(() => {
    if (key) window.localStorage.setItem(key, '1')
    setDismissed(true)
  }, [key])

  return { dismissed, dismiss }
}
