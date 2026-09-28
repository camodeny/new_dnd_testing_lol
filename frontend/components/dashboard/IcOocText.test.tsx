import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import IcOocText from './IcOocText'

describe('IcOocText', () => {
  it('wraps IC segments in the highlight span and keeps OOC plain', () => {
    const html = renderToStaticMarkup(
      <IcOocText
        message={{
          id: 'm1',
          session_id: 's1',
          role: 'player',
          content: 'I say "Hello" loudly',
          created_at: new Date(0).toISOString(),
          segments: [
            { type: 'ooc', text: 'I say ' },
            { type: 'ic', text: 'Hello' },
            { type: 'ooc', text: ' loudly' },
          ],
        }}
      />,
    )
    expect(html).toContain('class="session-ic-span"')
    expect(html).toContain('Hello')
    expect(html).toContain('I say ')
  })

  it('falls back to plain text without segments', () => {
    const html = renderToStaticMarkup(
      <IcOocText
        message={{
          id: 'm2',
          session_id: 's1',
          role: 'player',
          content: 'plain table talk',
          created_at: new Date(0).toISOString(),
        }}
      />,
    )
    expect(html).not.toContain('session-ic-span')
    expect(html).toContain('plain table talk')
  })
})
