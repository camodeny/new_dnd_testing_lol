'use client'

import { createContext, useContext } from 'react'
import type { User } from '@/types'

interface AuthContextValue {
  user: User | null
  setUser: (user: User | null) => void
  loading: boolean
  logout: () => Promise<void>
}

export const AuthContext = createContext<AuthContextValue>({
  user: null,
  setUser: () => {},
  loading: true,
  logout: async () => {},
})

export function useAuthContext() {
  return useContext(AuthContext)
}
