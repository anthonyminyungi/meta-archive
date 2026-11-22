/** API client for read-only Meta Archive FastAPI backend. */

async function getJson(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const payload = await res.json();
      if (payload && payload.detail) detail = String(payload.detail);
    } catch {
      // ignore non-JSON error body
    }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function buildQuery(params = {}) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") {
      sp.set(k, String(v));
    }
  }
  const qs = sp.toString();
  return qs ? `?${qs}` : "";
}

export function mediaUrl(localPath) {
  if (!localPath) return null;
  const cleaned = String(localPath).replace(/^\/+/, "");
  const encoded = cleaned
    .split("/")
    .filter(Boolean)
    .map((seg) => encodeURIComponent(seg))
    .join("/");
  return encoded ? `/media/${encoded}` : null;
}

export const api = {
  health: () => getJson("/api/health"),
  overview: () => getJson("/api/overview"),
  profiles: (platform) => getJson(`/api/profiles${buildQuery({ platform })}`),
  posts: (params) => getJson(`/api/posts${buildQuery(params)}`),
  postDetail: (postId) => getJson(`/api/posts/${encodeURIComponent(postId)}`),
  highlights: (platform = "instagram") => getJson(`/api/highlights${buildQuery({ platform })}`),
  stories: (params) => getJson(`/api/stories${buildQuery(params)}`),
  connections: (params) => getJson(`/api/connections${buildQuery(params)}`),
  albums: (platform = "facebook") => getJson(`/api/albums${buildQuery({ platform })}`),
  timeline: (platform = "facebook") => getJson(`/api/timeline${buildQuery({ platform })}`),
  threads: (params) => getJson(`/api/threads${buildQuery(params)}`),
  threadMessages: (threadId, params) =>
    getJson(`/api/threads/${encodeURIComponent(threadId)}/messages${buildQuery(params)}`),
  activities: (params) => getJson(`/api/activities${buildQuery(params)}`),
  search: (params) => getJson(`/api/search${buildQuery(params)}`),
  person: (identifier, limit = 50) =>
    getJson(`/api/people/${encodeURIComponent(identifier)}${buildQuery({ limit })}`),
};
