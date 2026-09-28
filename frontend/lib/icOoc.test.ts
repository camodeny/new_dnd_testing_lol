import { describe, expect, it } from 'vitest'
import { hasIcSpan, parseQuotedSegments, tokenizeForHighlight } from './icOoc'

describe('parseQuotedSegments', () => {
  it('treats plain text as a single OOC segment', () => {
    expect(parseQuotedSegments('I check the door')).toEqual([
      { type: 'ooc', text: 'I check the door' },
    ])
  })

  it('marks straight-quoted speech as IC and strips delimiters', () => {
    expect(parseQuotedSegments('I say "Hello there" loudly')).toEqual([
      { type: 'ooc', text: 'I say ' },
      { type: 'ic', text: 'Hello there' },
      { type: 'ooc', text: ' loudly' },
    ])
  })

  it('supports curly quotes', () => {
    expect(parseQuotedSegments('“For Gondor!” I charge')).toEqual([
      { type: 'ic', text: 'For Gondor!' },
      { type: 'ooc', text: ' I charge' },
    ])
  })

  it('handles multiple quoted spans in order', () => {
    expect(parseQuotedSegments('"Hi," I say, "follow me."')).toEqual([
      { type: 'ic', text: 'Hi,' },
      { type: 'ooc', text: ' I say, ' },
      { type: 'ic', text: 'follow me.' },
    ])
  })

  it('keeps an unmatched opener as forgiving OOC text', () => {
    expect(parseQuotedSegments('I say "hello')).toEqual([
      { type: 'ooc', text: 'I say "hello' },
    ])
    expect(hasIcSpan('I say "hello')).toBe(false)
  })

  it('keeps empty quotes as literal OOC text', () => {
    expect(parseQuotedSegments('table "" talk')).toEqual([
      { type: 'ooc', text: 'table "" talk' },
    ])
  })

  it('never emits empty segment text', () => {
    for (const segments of [
      parseQuotedSegments('""'),
      parseQuotedSegments('"a"'),
      parseQuotedSegments('plain'),
    ]) {
      expect(segments.length).toBeGreaterThan(0)
      for (const segment of segments) expect(segment.text).not.toBe('')
    }
  })
})

describe('tokenizeForHighlight', () => {
  it('keeps delimiters on IC tokens for the composer overlay', () => {
    expect(tokenizeForHighlight('Say "hi" now')).toEqual([
      { text: 'Say ', ic: false },
      { text: '"hi"', ic: true },
      { text: ' now', ic: false },
    ])
  })
})
