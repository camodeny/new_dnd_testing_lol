'use client'

import { useSyncExternalStore } from 'react'

// Plain predicate for one-shot checks (effects, event handlers). False
// without a browser or without matchMedia (jsdom).
export function matchesMedia(query: string): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia(query).matches
}

// SSR-safe viewport match: false on the server (and before hydration),
// live value in the browser. Guards the jsdom case (no matchMedia).
// Prefer CSS when CSS can do the job; use this only when JS must branch
// on a breakpoint (e.g. hiding chrome that would otherwise overflow a
// phone screen before CSS can hide it).
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const list = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
        ? window.matchMedia(query)
        : null
      if (!list) return () => {}
      list.addEventListener('change', onChange)
      return () => list.removeEventListener('change', onChange)
    },
    () => matchesMedia(query),
    () => false,
  )
}
