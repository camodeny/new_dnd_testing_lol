import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import DmNarration from './DmNarration'

describe('DmNarration', () => {
  it('renders NPC lines as speaker-labelled dialogue blocks between prose', () => {
    const html = renderToStaticMarkup(
      <DmNarration content={'The rider leans forward.\n\n<npc speaker="Rust-cloaked rider">"The debt is due."</npc>\n\nWhat does Brannik do?'} />,
    )
    expect(html).toContain('<figcaption class="npc-line-speaker">Rust-cloaked rider</figcaption>')
    expect(html).toContain('<blockquote class="npc-line-text"><div class="markdown-content"><p>&quot;The debt is due.&quot;</p></div></blockquote>')
    expect(html.indexOf('leans forward')).toBeLessThan(html.indexOf('npc-line'))
    expect(html.indexOf('npc-line')).toBeLessThan(html.indexOf('What does Brannik do?'))
    expect(html).not.toContain('&lt;npc')
  })

  it('renders a speaker name as text, never as markup', () => {
    const html = renderToStaticMarkup(<DmNarration content={'<npc speaker="&lt;b&gt;Mara&lt;/b&gt;">"Hi."</npc>'} />)
    expect(html).toContain('&lt;b&gt;Mara&lt;/b&gt;')
    expect(html).not.toContain('<b>Mara</b>')
  })
})
