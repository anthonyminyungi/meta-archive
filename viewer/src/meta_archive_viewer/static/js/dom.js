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
  if (media.media_type === "video") return true;
  const p = (media.local_path || "").toLowerCase();
  return p.endsWith(".mp4") || p.endsWith(".mov") || p.endsWith(".webm");
}

export function isAudioMedia(media) {
  if (!media) return false;
  if (media.media_type === "audio") return true;
  const p = (media.local_path || "").toLowerCase();
  return p.endsWith(".m4a") || p.endsWith(".mp3") || p.endsWith(".ogg") || p.endsWith(".wav");
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
