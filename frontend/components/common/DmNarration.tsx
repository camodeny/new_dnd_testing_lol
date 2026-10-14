'use client'

import MarkdownContent from '@/components/common/MarkdownContent'
import { parseDmNarration } from '@/lib/npcDialogue'

/**
 * DM narration body: markdown prose, with each tagged NPC line rendered as
 * its own dialogue block under the speaker's name.
 */
export default function DmNarration({ content }: { content: string }) {
  return (
    <div className="dm-narration">
      {parseDmNarration(content).map((part, index) =>
        part.type === 'npc' ? (
          <figure className="npc-line" key={index}>
            {part.speaker && <figcaption className="npc-line-speaker">{part.speaker}</figcaption>}
            <blockquote className="npc-line-text">
              <MarkdownContent content={part.text} />
            </blockquote>
          </figure>
        ) : (
          <MarkdownContent content={part.text} key={index} />
        ),
      )}
    </div>
  )
}
