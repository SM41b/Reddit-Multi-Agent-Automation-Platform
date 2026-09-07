import { useState, useEffect, useCallback } from 'react'
import { Bot, Activity } from 'lucide-react'
import { redditApi } from '../services/redditApi'

const STATUS_COLORS = {
  idle: 'bg-gray-100 text-gray-600',
  working: 'bg-blue-100 text-blue-700',
  completed: 'bg-green-100 text-green-700',
  error: 'bg-red-100 text-red-700',
}

const PersonaStatusPanel = ({ personaId }) => {
  const [agents, setAgents] = useState({})
  const [error, setError] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const res = await redditApi.getPersonaStatus(personaId)
      setAgents(res.agents || {})
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }, [personaId])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, 10000)
    return () => clearInterval(interval)
  }, [refresh])

  return (
    <div className="space-y-3">
      <h3 className="text-lg font-semibold flex items-center gap-2">
        <Activity className="w-5 h-5" />
        Persona: {personaId}
      </h3>
      {error && <div className="text-sm text-red-600">{error}</div>}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {Object.entries(agents).map(([name, status]) => (
          <div key={name} className="border rounded-lg p-3 bg-white shadow-sm">
            <div className="flex items-center gap-2 mb-1">
              <Bot className="w-4 h-4 text-gray-500" />
              <span className="font-medium">{name}</span>
            </div>
            <span
              className={`inline-block text-xs px-2 py-0.5 rounded-full ${
                STATUS_COLORS[status.status] || STATUS_COLORS.idle
              }`}
            >
              {status.status}
            </span>
            {status.current_task && (
              <p className="text-xs text-gray-400 mt-1 truncate">Task: {status.current_task}</p>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

export default PersonaStatusPanel