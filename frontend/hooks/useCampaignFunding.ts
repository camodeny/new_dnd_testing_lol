'use client'

/**
 * Campaign add-funds flow hook — issue #256.
 *
 * Starts a server-side Stripe checkout and hands off to the Stripe-hosted
 * page (card data never touches this app), then resyncs from the
 * authoritative capacity projection when the payer returns to the same
 * live table. Success-page state alone never credits anything: the status
 * read reconciles against Stripe server-side, and capacity refresh comes
 * from the #255 projection hook owned by the caller.
 */

import { useCallback, useState } from 'react'
import {
  buildFundingReturnUrls,
  listFundingOperations,
  newestPendingOperation,
  pollFundingOperation,
  startFundingCheckout,
  type FundingOperationStatus,
} from '@/lib/funding'
import type { CapacityUiEvent } from '@/lib/capacity'

interface UseCampaignFundingOptions {
  campaignId: string | null
  onEvent?: (event: CapacityUiEvent) => void
  /** Navigation seam (tests inject a spy; defaults to a full redirect). */
  navigate?: (url: string) => void
  /** Key seam for deterministic tests. */
  newKey?: () => string
}

export interface CampaignFunding {
  starting: boolean
  error: string | null
  startAddFunds: (amountCents: number) => Promise<void>
  syncAfterReturn: () => Promise<FundingOperationStatus | null>
}

function defaultKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export function useCampaignFunding({
  campaignId,
  onEvent,
  navigate,
  newKey = defaultKey,
}: UseCampaignFundingOptions): CampaignFunding {
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const startAddFunds = useCallback(async (amountCents: number) => {
    if (!campaignId || starting) return
    setStarting(true)
    setError(null)
    try {
      const origin = typeof window !== 'undefined' ? window.location.origin : ''
      const urls = buildFundingReturnUrls(origin, campaignId)
      const key = newKey()
      const result = await startFundingCheckout(campaignId, amountCents, urls, key)
      onEvent?.({ type: 'resume_choice', path: 'add_funds' })
      const go = navigate ?? ((url: string) => { window.location.assign(url) })
      go(result.checkout_url)
    } catch (err) {
      setError((err as Error).message || 'Checkout could not start. Nothing was charged.')
    } finally {
      setStarting(false)
    }
  }, [campaignId, starting, navigate, newKey, onEvent])

  const syncAfterReturn = useCallback(async (): Promise<FundingOperationStatus | null> => {
    if (!campaignId) return null
    // Recover the operation awaiting payment (browser closed or returned
    // without an operation id): newest pending from the member-safe list.
    const { funding_operations } = await listFundingOperations(campaignId)
    const pending = newestPendingOperation(funding_operations)
    if (!pending) return null
    const final = await pollFundingOperation(campaignId, pending.id)
    return final.status
  }, [campaignId])

  return { starting, error, startAddFunds, syncAfterReturn }
}
