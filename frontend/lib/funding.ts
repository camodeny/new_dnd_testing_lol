/**
 * Campaign add-funds client — issue #256.
 *
 * Stripe-hosted checkout only: card data is entered on Stripe's pages and
 * never touches this app. These helpers start a server-side checkout,
 * redirect there, and — on return to the same live table — poll the
 * member-safe operation status. Status reads reconcile against Stripe
 * server-side; redirect/success-page state alone never credits capacity.
 *
 * Amounts are integer cents. Client presets are UX only; the server
 * validates every amount before creating anything.
 */

import { apiFetch } from '@/lib/api'

/** Member-safe funding-operation projection (no Stripe ids, no payment details). */
export interface FundingOperation {
  id: string
  campaign_id: string
  amount_cents: number
  currency: string
  status: FundingOperationStatus
  /** One-time Stripe-hosted checkout URL, present only while pending. */
  checkout_url: string | null
  created_at: string | null
  confirmed_at: string | null
}

export type FundingOperationStatus = 'pending' | 'confirmed' | 'failed' | 'canceled'

export function isTerminalFundingStatus(status: FundingOperationStatus): boolean {
  return status === 'confirmed' || status === 'failed' || status === 'canceled'
}

/** Checkout amount presets in cents. Server bounds stay authoritative. */
export const FUNDING_PRESETS_CENTS = [500, 1000, 2500] as const
export const DEFAULT_FUNDING_CENTS = 1000

export function formatFundingAmount(amountCents: number, currency = 'USD'): string {
  const value = amountCents / 100
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(value)
  } catch {
    return `${value.toFixed(2)} ${currency}`
  }
}

/** Absolute Stripe redirect targets that return to the same live table. */
export function buildFundingReturnUrls(
  origin: string,
  campaignId: string,
): { successUrl: string; cancelUrl: string } {
  const cleanOrigin = origin.replace(/\/+$/, '')
  const base = `${cleanOrigin}/campaigns/${encodeURIComponent(campaignId)}`
  return {
    successUrl: `${base}?funding=success`,
    cancelUrl: `${base}?funding=cancel`,
  }
}

export interface FundingCheckoutResult {
  funding_operation: FundingOperation
  checkout_url: string
}

export function startFundingCheckout(
  campaignId: string,
  amountCents: number,
  urls: { successUrl: string; cancelUrl: string },
  idempotencyKey: string,
): Promise<FundingCheckoutResult> {
  return apiFetch<FundingCheckoutResult>(`/campaigns/${campaignId}/funding/checkout`, {
    method: 'POST',
    body: JSON.stringify({
      amount_cents: amountCents,
      success_url: urls.successUrl,
      cancel_url: urls.cancelUrl,
      operation_id: idempotencyKey,
    }),
    headers: { 'Idempotency-Key': idempotencyKey },
  })
}

export interface FundingStatusResult {
  funding_operation: FundingOperation
  capacity: unknown
}

export function getFundingOperation(
  campaignId: string,
  operationId: string,
): Promise<FundingStatusResult> {
  return apiFetch<FundingStatusResult>(
    `/campaigns/${campaignId}/funding/operations/${encodeURIComponent(operationId)}`,
  )
}

export function listFundingOperations(
  campaignId: string,
): Promise<{ funding_operations: FundingOperation[] }> {
  return apiFetch<{ funding_operations: FundingOperation[] }>(
    `/campaigns/${campaignId}/funding/operations`,
  )
}

export interface PollFundingOptions {
  timeoutMs?: number
  intervalMs?: number
  /** Overrideable sleep for tests. */
  sleep?: (ms: number) => Promise<void>
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => { setTimeout(resolve, ms) })

/**
 * Poll a funding operation until it reaches a terminal status.
 * Read-only client side: confirmation happens server-side against Stripe.
 */
export async function pollFundingOperation(
  campaignId: string,
  operationId: string,
  options: PollFundingOptions = {},
): Promise<FundingOperation> {
  const { timeoutMs = 60_000, intervalMs = 2_000, sleep = defaultSleep } = options
  const deadline = Date.now() + timeoutMs
  let last: FundingOperation | null = null
  for (;;) {
    const result = await getFundingOperation(campaignId, operationId)
    last = result.funding_operation
    if (isTerminalFundingStatus(last.status)) return last
    if (Date.now() >= deadline) return last
    await sleep(intervalMs)
  }
}

/** Newest operation still awaiting payment, if any (Stripe-redirect recovery). */
export function newestPendingOperation(
  operations: FundingOperation[] | null | undefined,
): FundingOperation | null {
  if (!operations || operations.length === 0) return null
  const pending = operations.filter((op) => op.status === 'pending')
  if (pending.length === 0) return null
  return pending.reduce((a, b) =>
    String(a.created_at ?? '') >= String(b.created_at ?? '') ? a : b,
  )
}
