'use client'

import { useAuth } from '@/hooks/useAuth'
import { AuthContext } from '@/contexts/AuthContext'

export default function AuthProvider({ children }: { children: React.ReactNode }) {
  const auth = useAuth()
  return <AuthContext.Provider value={auth}>{children}</AuthContext.Provider>
}
