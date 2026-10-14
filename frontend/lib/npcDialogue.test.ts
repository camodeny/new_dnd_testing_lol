import { describe, expect, it } from 'vitest'
import { parseDmNarration } from './npcDialogue'

describe('parseDmNarration', () => {
  it('splits prose and tagged NPC lines in order', () => {
    expect(
      parseDmNarration(
        'The rider leans forward.\n\n<npc speaker="Rust-cloaked rider">"The debt is due."</npc>\n\nWhat does Brannik do?',
      ),
    ).toEqual([
      { type: 'prose', text: 'The rider leans forward.\n\n' },
      { type: 'npc', speaker: 'Rust-cloaked rider', text: '"The debt is due."' },
      { type: 'prose', text: '\n\nWhat does Brannik do?' },
    ])
  })

  it('leaves narration without dialogue as a single prose part', () => {
    expect(parseDmNarration('Evening settles over the crossing.')).toEqual([
      { type: 'prose', text: 'Evening settles over the crossing.' },
    ])
  })

  it('decodes escaped speaker names and tolerates a missing speaker', () => {
    expect(parseDmNarration('<npc speaker="Bram &quot;Two-Fingers&quot; Holt">"Pay up."</npc><npc>"Now."</npc>')).toEqual([
      { type: 'npc', speaker: 'Bram "Two-Fingers" Holt', text: '"Pay up."' },
      { type: 'npc', speaker: '', text: '"Now."' },
    ])
  })

  it('renders a line still streaming in and hides a half-written tag', () => {
    expect(parseDmNarration('The rider speaks.\n\n<npc speaker="Rider">"The debt is du')).toEqual([
      { type: 'prose', text: 'The rider speaks.\n\n' },
      { type: 'npc', speaker: 'Rider', text: '"The debt is du' },
    ])
    expect(parseDmNarration('<npc speaker="Rider">"The debt is due."</np')).toEqual([
      { type: 'npc', speaker: 'Rider', text: '"The debt is due."' },
    ])
    expect(parseDmNarration('The rider speaks.\n\n<npc spea')).toEqual([
      { type: 'prose', text: 'The rider speaks.\n\n' },
    ])
    expect(parseDmNarration('The rider speaks.\n\n<npc speaker="Rider">')).toEqual([
      { type: 'prose', text: 'The rider speaks.\n\n' },
      { type: 'npc', speaker: 'Rider', text: '' },
    ])
  })

  it('never shows stray tags as text', () => {
    expect(parseDmNarration('The rider speaks.</npc> Silence.')).toEqual([
      { type: 'prose', text: 'The rider speaks. Silence.' },
    ])
  })
})
