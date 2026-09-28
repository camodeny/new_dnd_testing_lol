'use client'

import type { Message } from '@/types'

/**
 * Player message body with mixed IC/OOC rendering. IC spans (quoted speech
 * at send time) get a colored box; OOC table talk stays plain. Messages
 * without typed segments fall back to plain text.
 */
export default function IcOocText({ message }: { message: Message }) {
  const segments = Array.isArray(message.segments)
    ? message.segments.filter(
        (segment) =>
          !!segment
          && (segment.type === 'ic' || segment.type === 'ooc')
          && typeof segment.text === 'string'
          && segment.text.length > 0,
      )
    : []
  if (segments.length === 0) {
    return <p style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{message.content}</p>
  }
  return (
    <p style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
      {segments.map((segment, index) =>
        segment.type === 'ic' ? (
          <q key={index} className="session-ic-span">
            {segment.text}
          </q>
        ) : (
          <span key={index}>{segment.text}</span>
        ),
      )}
    </p>
  )
}
