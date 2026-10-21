'use client'

import { useCallback, useEffect, useRef, useState } from 'react'

/** Within this distance of the end still counts as "at the latest". */
const NEAR_BOTTOM_PX = 96

/**
 * Keeps a conversation pinned to its newest entry while the reader is at
 * the bottom: new messages, streaming narration, and a shrinking container
 * (the phone keyboard opening) all keep the latest line in view. Scrolling
 * up to re-read unpins it, so streaming never yanks the reader back down;
 * `away` tells the UI to offer a way back.
 *
 * Only the container itself scrolls. scrollIntoView would also scroll
 * every ancestor, including overflow-hidden app frames, sliding fixed
 * chrome off-screen on phones.
 *
 * `scrollRef`/`contentRef` are callback refs, so a remounted container
 * (e.g. the story moving into the combat layout) is picked up.
 */
export function useStickToBottom<S extends HTMLElement = HTMLDivElement, C extends HTMLElement = HTMLDivElement>() {
  const [scrollEl, scrollRef] = useState<S | null>(null)
  const [contentEl, contentRef] = useState<C | null>(null)
  const pinned = useRef(true)
  const [away, setAway] = useState(false)

  const setPinned = useCallback((next: boolean) => {
    pinned.current = next
    setAway(!next)
  }, [])

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    if (!scrollEl) return
    setPinned(true)
    scrollEl.scrollTo({ top: scrollEl.scrollHeight, behavior })
  }, [scrollEl, setPinned])

  /** Stop following, e.g. while older history is prepended above. */
  const unpin = useCallback(() => setPinned(false), [setPinned])

  /** Re-derive pinning from where the reader actually is, after a
   *  programmatic jump that fires no scroll event (or nothing to scroll). */
  const sync = useCallback(() => {
    if (!scrollEl) return
    setPinned(scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight <= NEAR_BOTTOM_PX)
  }, [scrollEl, setPinned])

  useEffect(() => {
    const el = scrollEl
    if (!el) return
    let lastTop = el.scrollTop
    const onScroll = () => {
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX
      // Only the reader moving up unpins; a smooth scroll down to the
      // latest passes through "not at bottom" without letting go.
      if (atBottom && !pinned.current) setPinned(true)
      else if (!atBottom && el.scrollTop < lastTop && pinned.current) setPinned(false)
      lastTop = el.scrollTop
    }
    const follow = () => {
      if (!pinned.current) return
      el.scrollTop = el.scrollHeight
      lastTop = el.scrollTop
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(follow) : null
    observer?.observe(el)
    if (contentEl) observer?.observe(contentEl)
    follow()
    return () => {
      el.removeEventListener('scroll', onScroll)
      observer?.disconnect()
    }
  }, [scrollEl, contentEl, setPinned])

  return { scrollRef, contentRef, scrollEl, away, scrollToBottom, unpin, sync }
}
