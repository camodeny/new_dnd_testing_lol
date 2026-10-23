'use client'

import { useEffect } from 'react'

/**
 * Publishes the visual viewport as CSS variables on <html>:
 * `--vv-height` (visible height) and `--vv-top` (how far the browser has
 * panned it down the layout viewport).
 *
 * iOS Safari keeps the layout viewport full height when the on-screen
 * keyboard opens and pans the page instead, so a fixed full-screen surface
 * sized with vh/dvh slides its header off the top and keeps its composer
 * under the keyboard. Such surfaces use
 * `top: var(--vv-top, 0px); height: var(--vv-height, 100dvh)` to track
 * what is actually visible. Android Chrome resizes the layout viewport
 * itself (interactive-widget=resizes-content), where these simply match.
 */
export function useVisualViewportVars() {
  useEffect(() => {
    const vv = window.visualViewport
    if (!vv) return
    const root = document.documentElement
    let frame = 0
    const update = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        // Pinch zoom also shrinks the visual viewport; resizing the app
        // under the reader's fingers would fight the zoom.
        if (vv.scale > 1.01) return
        root.style.setProperty('--vv-height', `${Math.round(vv.height)}px`)
        root.style.setProperty('--vv-top', `${Math.round(vv.offsetTop)}px`)
      })
    }
    update()
    vv.addEventListener('resize', update)
    vv.addEventListener('scroll', update)
    return () => {
      cancelAnimationFrame(frame)
      vv.removeEventListener('resize', update)
      vv.removeEventListener('scroll', update)
      root.style.removeProperty('--vv-height')
      root.style.removeProperty('--vv-top')
    }
  }, [])
}
