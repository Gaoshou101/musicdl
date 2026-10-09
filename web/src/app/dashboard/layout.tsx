'use client'

import { useEffect, useState } from 'react'
import { usePathname, useRouter } from 'next/navigation'
import { Sidebar } from '@/components/Sidebar'
import { bootstrapAdminSession } from '@/lib/api'

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const pathname = usePathname()
  const router = useRouter()
  const [ready, setReady] = useState(false)

  useEffect(() => {
    let active = true
    void bootstrapAdminSession()
      .then((session) => {
        if (!active) return
        if (!session.authenticated) {
          router.replace('/')
          return
        }
        if (session.must_change && pathname !== '/dashboard/settings') {
          router.replace('/dashboard/settings?must_change=1')
          return
        }
        setReady(true)
      })
      .catch(() => {
        if (active) router.replace('/')
      })
    return () => { active = false }
  }, [pathname, router])

  if (!ready) {
    return (
      <div className="flex h-screen items-center justify-center bg-neutral-950 text-sm text-neutral-400">
        正在恢复登录状态…
      </div>
    )
  }

  return (
    <div className="flex h-screen min-h-screen overflow-hidden bg-neutral-950">
      <Sidebar />
      <main className="min-w-0 flex-1 overflow-y-auto">
        {children}
      </main>
    </div>
  )
}
