import { createClient } from '@supabase/supabase-js'

const resolvedUrl = process.env.NEXT_PUBLIC_SUPABASE_URL ?? ''
const rawPublishableKey = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ?? ''

// Use placeholder at build-time so `next build` prerender doesn't throw.
// Runtime calls will still check isSupabaseConfigured() before using auth.
const supabaseUrl = resolvedUrl || 'https://placeholder.supabase.co'
const supabaseKey = rawPublishableKey || 'placeholder-publishable-key'

if (!resolvedUrl || !rawPublishableKey) {
  if (typeof window !== 'undefined') {
    console.warn('[supabase] NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY not set')
  }
}

export const supabase = createClient(supabaseUrl, supabaseKey, {
  auth: {
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: true,
  },
})

export function isSupabaseConfigured(): boolean {
  return Boolean(resolvedUrl && rawPublishableKey)
}
