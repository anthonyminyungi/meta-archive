/** API client with in-memory LRU caching, in-flight deduplication, and image preloading. */

const MAX_CACHE_ENTRIES = 240;
const responseCache = new Map();
const inFlight = new Map();

const MAX_IMAGE_PRELOADS = 100;
const preloadedImages = new Map();

function setCache(key, value) {
  if (responseCache.has(key)) {
    responseCache.delete(key);
  }
  responseCache.set(key, value);
  if (responseCache.size > MAX_CACHE_ENTRIES) {
    const oldest = responseCache.keys().next().value;
    if (oldest !== undefined) responseCache.delete(oldest);
  }
}

async function fetchJsonRaw(path) {
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

function getJson(path, { cache = true } = {}) {
  if (cache && responseCache.has(path)) {
    const hit = responseCache.get(path);
    // Refresh LRU recency
    responseCache.delete(path);
    responseCache.set(path, hit);
    return Promise.resolve(hit);
  }
  if (inFlight.has(path)) {
    return inFlight.get(path);
  }
  const req = fetchJsonRaw(path)
    .then((data) => {
      if (cache) setCache(path, data);
      return data;
    })
    .finally(() => {
      inFlight.delete(path);
    });
  inFlight.set(path, req);
  return req;
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

/** Preload and decode an image into browser cache for zero-flicker slide swaps. */
export function preloadImage(src) {
  if (!src) return Promise.resolve(null);
  if (preloadedImages.has(src)) {
    return preloadedImages.get(src);
  }
  const p = new Promise((resolve) => {
    const img = new Image();
    img.decoding = "async";
    img.src = src;
    if (typeof img.decode === "function") {
      img
        .decode()
        .then(() => resolve(img))
        .catch(() => resolve(img));
    } else {
      img.onload = () => resolve(img);
      img.onerror = () => resolve(null);
    }
  });
  preloadedImages.set(src, p);
  if (preloadedImages.size > MAX_IMAGE_PRELOADS) {
    const oldest = preloadedImages.keys().next().value;
    if (oldest !== undefined) preloadedImages.delete(oldest);
  }
  return p;
}

export const api = {
  health: () => getJson("/api/health", { cache: false }),
  overview: () => getJson("/api/overview"),
  profiles: (platform) => getJson(`/api/profiles${buildQuery({ platform })}`),
  posts: (params) => getJson(`/api/posts${buildQuery(params)}`),
  postDetail: (postId) => getJson(`/api/posts/${encodeURIComponent(postId)}`),
  prefetchPost: (postId) => {
    if (!postId) return;
    getJson(`/api/posts/${encodeURIComponent(postId)}`).catch(() => {});
  },
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
