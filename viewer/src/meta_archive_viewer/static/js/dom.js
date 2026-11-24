/** Safe DOM builders and formatting helpers (strictly textContent, zero unescaped innerHTML). */

import { mediaUrl } from "./api.js?v=2";

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

function parseDateObj(createdAt, timestampUnix) {
  if (createdAt) {
    const parsed = new Date(createdAt);
    if (!Number.isNaN(parsed.getTime())) return parsed;
  }
  if (timestampUnix) {
    const parsed = new Date(Number(timestampUnix) * 1000);
    if (!Number.isNaN(parsed.getTime())) return parsed;
  }
  return null;
}

export function formatDate(createdAt, timestampUnix) {
  const d = parseDateObj(createdAt, timestampUnix);
  if (!d) return "날짜 정보 없음";
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(d);
}

export function formatShortDate(createdAt, timestampUnix) {
  const d = parseDateObj(createdAt, timestampUnix);
  if (!d) return "";
  return new Intl.DateTimeFormat("ko-KR", {
    year: "2-digit",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
}

export function formatTimeOnly(createdAt, timestampUnix) {
  const d = parseDateObj(createdAt, timestampUnix);
  if (!d) return "";
  return new Intl.DateTimeFormat("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(d);
}

export function formatDayKey(createdAt, timestampUnix) {
  const d = parseDateObj(createdAt, timestampUnix);
  if (!d) return "";
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function formatDayLabel(createdAt, timestampUnix) {
  const d = parseDateObj(createdAt, timestampUnix);
  if (!d) return "날짜 정보 없음";
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "short",
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

function hashColorIndex(label) {
  const str = String(label || "?");
  let h = 0;
  for (let i = 0; i < str.length; i += 1) {
    h = (h * 31 + str.charCodeAt(i)) >>> 0;
  }
  return h % 8;
}

export function avatarEl(localPath, label = "?", sizeOrLarge = false) {
  let sizeCls = "avatar-md";
  if (sizeOrLarge === true || sizeOrLarge === "lg") sizeCls = "avatar-lg";
  else if (sizeOrLarge === "sm") sizeCls = "avatar-sm";
  const cls = `avatar ${sizeCls}`;
  const src = mediaUrl(localPath);
  if (src) {
    return el("img", { className: cls, src, alt: label, loading: "lazy" });
  }
  const initial = (String(label || "?").trim().slice(0, 1) || "?").toUpperCase();
  const colorCls = `av-c${hashColorIndex(label)}`;
  return el("span", { className: `${cls} avatar-fallback ${colorCls}`, "aria-hidden": "true" }, initial);
}

export function personButton(username, fullName, onOpenPerson) {
  const label =
    fullName && username && fullName !== username
      ? `${fullName} (@${username})`
      : fullName || (username ? `@${username}` : "알 수 없음");
  const identifier = username || fullName;
  if (!identifier || !onOpenPerson) {
    return el("span", { style: "font-weight:600;" }, label);
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

const REACTION_EMOJI_MAP = {
  LIKE: "👍",
  "좋아요": "👍",
  LOVE: "❤️",
  "최고예요": "❤️",
  HAHA: "😆",
  "웃겨요": "😆",
  WOW: "😮",
  "멋져요": "😮",
  SAD: "😢",
  "슬퍼요": "😢",
  ANGRY: "😠",
  "화나요": "😠",
  CARE: "🤗",
  "힘내요": "🤗",
};

export function formatReactionBadge(rtype, count) {
  const emoji = REACTION_EMOJI_MAP[rtype] || REACTION_EMOJI_MAP[String(rtype).toUpperCase()] || "👍";
  return `${emoji} ${rtype} ${count}`;
}

export function renderCollapsibleText(text, { maxChars = 320, maxLines = 6, className = "post-caption" } = {}) {
  if (!text) return null;
  const lines = String(text).split("\n");
  const needsClamp = text.length > maxChars || lines.length > maxLines;
  if (!needsClamp) {
    return el("div", { className }, text);
  }
  const truncated =
    lines.length > maxLines
      ? `${lines.slice(0, maxLines).join("\n").slice(0, maxChars)}…`
      : `${text.slice(0, maxChars)}…`;

  let expanded = false;
  const textNode = el("div", { className, style: "margin-bottom:4px;" }, truncated);
  const toggleBtn = el(
    "button",
    {
      type: "button",
      className: "btn-link caption-toggle-btn",
      onClick: (ev) => {
        ev.stopPropagation();
        expanded = !expanded;
        textNode.textContent = expanded ? text : truncated;
        toggleBtn.textContent = expanded ? "접기" : "더 보기";
      },
    },
    "더 보기"
  );
  return el("div", null, textNode, toggleBtn);
}

export function highlightTextNodes(text, query) {
  const src = String(text || "");
  const q = String(query || "").trim();
  if (!q) return [document.createTextNode(src)];
  const lowerSrc = src.toLowerCase();
  const lowerQ = q.toLowerCase();
  const nodes = [];
  let pos = 0;
  while (pos < src.length) {
    const idx = lowerSrc.indexOf(lowerQ, pos);
    if (idx === -1) {
      nodes.push(document.createTextNode(src.slice(pos)));
      break;
    }
    if (idx > pos) {
      nodes.push(document.createTextNode(src.slice(pos, idx)));
    }
    nodes.push(el("mark", { className: "search-mark" }, src.slice(idx, idx + q.length)));
    pos = idx + q.length;
  }
  return nodes;
}

export function renderSharedCard(sharedPost, onOpenLightbox) {
  if (!sharedPost) return null;
  const thumbSrc = mediaUrl(sharedPost.thumbnail_local_path);
  const thumbEl = thumbSrc
    ? el("img", {
        className: "shared-card-banner",
        src: thumbSrc,
        alt: sharedPost.title || sharedPost.domain || "shared link thumbnail",
        loading: "lazy",
        onClick: onOpenLightbox
          ? (ev) => {
              ev.stopPropagation();
              onOpenLightbox([{ local_path: sharedPost.thumbnail_local_path, media_type: "image" }], 0);
            }
          : undefined,
      })
    : null;

  const metaRow =
    sharedPost.domain || sharedPost.author_name
      ? el(
          "div",
          { className: "shared-card-meta" },
          sharedPost.domain ? el("span", { className: "shared-card-domain" }, `🔗 ${sharedPost.domain}`) : null,
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

  const descNode = sharedPost.text
    ? renderCollapsibleText(sharedPost.text, {
        maxChars: 260,
        maxLines: 5,
        className: "shared-card-desc",
      })
    : null;

  return el(
    "div",
    { className: `shared-card${thumbEl ? "" : " shared-card-no-thumb"}` },
    thumbEl,
    el(
      "div",
      { className: "shared-card-body" },
      metaRow,
      sharedPost.title ? el("div", { className: "shared-card-title" }, sharedPost.title) : null,
      descNode,
      urlNode
    )
  );
}
