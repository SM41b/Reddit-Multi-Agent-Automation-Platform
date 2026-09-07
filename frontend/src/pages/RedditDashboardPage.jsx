import { useState, useEffect, useCallback } from 'react'
import { Radio } from 'lucide-react'
import { useWebSocket } from '../hooks/useWebSocket'
import PersonaStatusPanel from '../components/PersonaStatusPanel'
import ApprovalQueue from '../components/ApprovalQueue'

// Composes the persona status panel and approval queue for one persona,
// and uses the existing useWebSocket hook (unchanged, fully reusable)
// against the new /ws/reddit-dashboard bridge for live updates. On any
// incoming event this bumps a refreshKey — the child panels re-fetch via
// their own polling effect, which is simpler than threading push data
// through several levels of props for what's still a small dashboard.
const RedditDashboardPage = ({ personaId }) => {
  const [refreshKey, setRefreshKey] = useState(0)
  const [feed, setFeed] = useState([])

  const { data, isConnected } = useWebSocket('/ws/reddit-dashboard')

  useEffect(() => {
    if (!data) return
    setFeed((prev) => [data, ...prev].slice(0, 20))
    setRefreshKey((k) => k + 1)
  }, [data])

  if (!personaId) {
    return <div className="p-6 text-sm text-gray-500">Select a persona to view its dashboard.</div>
  }

  return (
    <div className="max-w-4xl mx-auto p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">Reddit Persona Dashboard</h2>
        <span className={`flex items-center gap-1 text-xs ${isConnected ? 'text-green-600' : 'text-gray-400'}`}>
          <Radio className="w-3 h-3" /> {isConnected ? 'Live' : 'Connecting…'}
        </span>
      </div>

      <PersonaStatusPanel key={`status-${refreshKey}`} personaId={personaId} />

      <ApprovalQueue key={`approvals-${refreshKey}`} personaId={personaId} />

      {feed.length > 0 && (
        <div className="border rounded-lg p-3 bg-gray-50">
          <h4 className="text-sm font-medium mb-2">Recent activity</h4>
          <ul className="space-y-1 text-xs text-gray-600">
            {feed.map((event, i) => (
              <li key={i}>
                <span className="font-mono text-gray-400">
                  {new Date(event.timestamp).toLocaleTimeString()}
                </span>{' '}
                — {event.message}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

export default RedditDashboardPage