// API client for the Reddit orchestrator backend — new file, sibling to
// the existing services/api.js. Kept separate rather than merged into
// that file since it's a large, business-feature-specific file and this
// is a self-contained set of endpoints.

const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `Request failed: ${res.status}`)
  }
  return res.json()
}

export const redditApi = {
  createPersona: (persona) =>
    request('/personas', { method: 'POST', body: JSON.stringify(persona) }),

  getPersonaStatus: (personaId) =>
    request(`/personas/${personaId}/status`),

  startWorkflow: (personaId, { taskBrief, userId = 'default', context = {} }) =>
    request(`/workflow/${personaId}/start`, {
      method: 'POST',
      body: JSON.stringify({ task_brief: taskBrief, user_id: userId, context }),
    }),

  getWorkflowJob: (personaId, jobId) =>
    request(`/workflow/${personaId}/jobs/${jobId}`),

  getPendingApprovals: (personaId) =>
    request(`/workflow/${personaId}/approvals`),

  respondToApproval: (personaId, approvalId, { threadId, decision, feedback = '', respondedBy = 'unknown' }) =>
    request(`/workflow/${personaId}/approvals/${approvalId}/respond`, {
      method: 'POST',
      body: JSON.stringify({
        thread_id: threadId,
        decision,
        feedback,
        responded_by: respondedBy,
      }),
    }),
}