import { useLocation, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard, CheckSquare, Puzzle, Brain, Network,
  Activity, Lightbulb, MessageSquare, Users, Settings, Clock,
} from 'lucide-react'

/**
 * live   = endpoint exists on the backend today
 * soon   = endpoint not yet built — page renders a placeholder state
 */
const NAV = [
  { label: 'Dashboard',       path: '/dashboard',       icon: LayoutDashboard, status: 'soon' as const },
  { label: 'Approvals',       path: '/approvals',       icon: CheckSquare,     status: 'live' as const },
  { label: 'Plugins',         path: '/plugins',         icon: Puzzle,          status: 'soon' as const },
  { label: 'Memory',          path: '/memory',          icon: Brain,           status: 'soon' as const },
  { label: 'Knowledge Graph', path: '/knowledge-graph', icon: Network,         status: 'soon' as const },
  { label: 'Activity',        path: '/activity',        icon: Activity,        status: 'soon' as const },
  { label: 'Findings',        path: '/findings',        icon: Lightbulb,       status: 'soon' as const },
  { label: 'Chat',            path: '/chat',            icon: MessageSquare,   status: 'soon' as const },
  { label: 'Specialists',     path: '/specialists',     icon: Users,           status: 'live' as const },
  { label: 'Settings',        path: '/settings',        icon: Settings,        status: 'soon' as const },
]

export default function Sidebar() {
  const location = useLocation()
  const navigate = useNavigate()

  return (
    <aside className="flex flex-col w-60 flex-shrink-0 h-full" style={{ background: '#142234' }}>
      {/* Brand */}
      <div className="px-6 py-5 border-b" style={{ borderColor: 'rgba(255,255,255,0.08)' }}>
        <div className="text-xs font-bold tracking-widest" style={{ color: '#3080bc' }}>MC &amp; S</div>
        <div className="text-white font-semibold text-base mt-0.5">
          CoWorker <span style={{ color: '#eb881f' }}>v3</span>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 py-3 overflow-y-auto">
        {NAV.map(({ label, path, icon: Icon, status }) => {
          const active = location.pathname === path
          return (
            <div
              key={path}
              onClick={() => navigate(path)}
              className={`sidebar-item${active ? ' active' : ''}`}
            >
              <div className="flex items-center gap-3">
                <Icon size={16} />
                <span>{label}</span>
              </div>
              {status === 'soon' && !active && (
                <span
                  className="flex items-center gap-0.5 text-xs px-1.5 py-0.5"
                  style={{ color: 'rgba(255,255,255,0.3)', fontSize: '10px' }}
                >
                  <Clock size={9} />
                  soon
                </span>
              )}
            </div>
          )
        })}
      </nav>

      {/* Footer */}
      <div className="px-6 py-4 border-t" style={{ borderColor: 'rgba(255,255,255,0.08)' }}>
        <div className="text-xs" style={{ color: 'rgba(255,255,255,0.3)' }}>
          CoWorker v3 · MC &amp; S Accountants
        </div>
      </div>
    </aside>
  )
}
