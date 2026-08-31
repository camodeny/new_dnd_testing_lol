import type { CampaignMember, CampaignThread } from '@/types'

export function otherThreadMemberId(thread: CampaignThread, currentUserId: string): string | null {
  return thread.members?.find((member) => member.user_id !== currentUserId)?.user_id ?? null
}

export function privateThreadLabel(
  thread: CampaignThread,
  members: CampaignMember[],
  currentUserId: string,
): string {
  if (thread.private_kind === 'dm') return 'AI DM'
  const otherId = otherThreadMemberId(thread, currentUserId)
  if (!otherId) return thread.title || 'Private conversation'
  return members.find((member) => member.user_id === otherId)?.username || 'Private player'
}

export function upsertVisibleThread(threads: CampaignThread[], thread: CampaignThread): CampaignThread[] {
  const index = threads.findIndex((candidate) => candidate.id === thread.id)
  if (index < 0) return [...threads, thread]
  return threads.map((candidate, candidateIndex) => candidateIndex === index ? thread : candidate)
}
