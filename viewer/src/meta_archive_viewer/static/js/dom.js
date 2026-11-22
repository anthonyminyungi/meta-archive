/** Safe DOM builders and formatting helpers (strictly textContent, zero unescaped innerHTML). */

import { mediaUrl } from "./api.js";

export function el(tag, attrs = null, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, val] of Object.entries(attrs)) {
      if (val === undefined || val === null || val === false) continue;
      if (key === "className") {
        node.className = val;
      } else if (key === "dataset" && typeof val === "object") {
        for (const [dk, dv] of Object.entries(val)) {
          if (dv !== undefined && dv !== null) node.dataset[dk] = String(dv);
        }
      } else if (key.startsWith("on") && typeof val === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), val);
      } else if (val === true) {
        node.setAttribute(key, "");
      } else {
        node.setAttribute(key, String(val));
      }
    }
  }
  appendChildren(node, children);
  return node;
}

export function appendChildren(parent, children) {
  for (const child of children) {
    if (child === undefined || child === null || child === false) continue;
    if (Array.isArray(child)) {
      appendChildren(parent, child);
    } else if (child instanceof Node) {
      parent.appendChild(child);
    } else {
      parent.appendChild(document.createTextNode(String(child)));
    }
  }
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

export function formatDate(createdAt, timestampUnix) {
  let d = null;
  if (createdAt) {
    const parsed = new Date(createdAt);
    if (!Number.isNaN(parsed.getTime())) d = parsed;
  }
  if (!d && timestampUnix) {
    const parsed = new Date(Number(timestampUnix) * 1000);
    if (!Number.isNaN(parsed.getTime())) d = parsed;
  }
  if (!d) return "날짜 정보 없음";
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(d);
}

export function formatDuration(seconds) {
  if (!seconds || Number.isNaN(Number(seconds))) return "";
  const total = Math.round(Number(seconds));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

export function isVideoMedia(media) {
  if (!media) return false;
  if (media.media_type === "audio" || media.media_type === "image") return false;
  if (media.media_type === "video") return true;
  const p = (media.local_path || "").toLowerCase();
  return p.endsWith(".mp4") || p.endsWith(".mov") || p.endsWith(".webm");
}

export function isAudioMedia(media) {
  if (!media) return false;
  if (media.media_type === "audio") return true;
  const p = (media.local_path || "").toLowerCase();
  return p.endsWith(".m4a") || p.endsWith(".mp3") || p.endsWith(".ogg") || p.endsWith(".wav") || p.endsWith(".opus");
}

export function isDocumentMedia(media) {
  if (!media) return false;
  if (isAudioMedia(media) || isVideoMedia(media) || media.media_type === "image") return false;
  if (media.media_type === "unknown") return true;
  const p = (media.local_path || "").toLowerCase();
  return (
    p.endsWith(".pdf") ||
    p.endsWith(".ppt") ||
    p.endsWith(".pptx") ||
    p.endsWith(".doc") ||
    p.endsWith(".docx") ||
    p.endsWith(".xls") ||
    p.endsWith(".xlsx") ||
    p.endsWith(".zip") ||
    p.endsWith(".hwp") ||
    p.endsWith(".txt")
  );
}

export function avatarEl(localPath, label = "?", large = false) {
  const cls = large ? "avatar avatar-lg" : "avatar";
  const src = mediaUrl(localPath);
  if (src) {
    return el("img", { className: cls, src, alt: label, loading: "lazy" });
  }
  const initial = (String(label || "?").trim().slice(0, 1) || "?").toUpperCase();
  return el("span", { className: `${cls} avatar-fallback` }, initial);
}

export function personButton(username, fullName, onOpenPerson) {
  const label = fullName && username && fullName !== username
    ? `${fullName} (@${username})`
    : fullName || (username ? `@${username}` : "알 수 없음");
  const identifier = username || fullName;
  if (!identifier || !onOpenPerson) {
    return el("span", null, label);
  }
  return el(
    "button",
    {
      type: "button",
      className: "btn-link",
      onClick: (ev) => {
        ev.stopPropagation();
        onOpenPerson(identifier);
      },
    },
    label
  );
}

export function safeExternalHref(rawUrl) {
  if (!rawUrl) return null;
  try {
    const parsed = new URL(String(rawUrl));
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

export function extractReactionEntries(reactions) {
  if (!reactions || typeof reactions !== "object") return [];
  const map = reactions.by_type || reactions.breakdown;
  if (!map || typeof map !== "object") return [];
  return Object.entries(map);
}

export function renderSharedCard(sharedPost, onOpenLightbox) {
  if (!sharedPost) return null;
  const thumbSrc = mediaUrl(sharedPost.thumbnail_local_path);
  const thumbEl = thumbSrc
    ? el("img", {
        className: "shared-card-thumb",
        src: thumbSrc,
        alt: sharedPost.title || sharedPost.domain || "shared link thumbnail",
        loading: "lazy",
        onClick: onOpenLightbox
          ? () => onOpenLightbox([{ local_path: sharedPost.thumbnail_local_path, media_type: "image" }], 0)
          : undefined,
      })
    : null;

  const metaRow =
    sharedPost.domain || sharedPost.author_name
      ? el(
          "div",
          { className: "shared-card-meta" },
          sharedPost.domain ? el("span", { className: "pill" }, sharedPost.domain) : null,
          sharedPost.author_name ? el("span", { className: "pill" }, `원작자: ${sharedPost.author_name}`) : null
        )
      : null;

  const href = safeExternalHref(sharedPost.url);
  const urlNode = sharedPost.url
    ? href
      ? el(
          "a",
          {
            className: "shared-card-url",
            href,
            target: "_blank",
            rel: "noopener noreferrer",
          },
          sharedPost.url
        )
      : el("div", { className: "shared-card-url" }, sharedPost.url)
    : null;

  return el(
    "div",
    { className: "shared-card" },
    thumbEl,
    el(
      "div",
      { className: "shared-card-body" },
      metaRow,
      sharedPost.title ? el("div", { className: "shared-card-title" }, sharedPost.title) : null,
      sharedPost.text ? el("div", { className: "shared-card-desc" }, sharedPost.text) : null,
      urlNode
    )
  );
}
