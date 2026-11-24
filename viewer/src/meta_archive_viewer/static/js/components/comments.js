/** Recursive nested comment/reply tree renderer with commenter avatars and clean speech bubbles. */

import { mediaUrl } from "../api.js?v=2";
import { avatarEl, el, formatDate, isAudioMedia, isVideoMedia, personButton } from "../dom.js?v=2";

function renderCommentNode(comment, onOpenPerson, onOpenLightbox) {
  const repliesNode =
    comment.replies && comment.replies.length > 0
      ? el(
          "div",
          { className: "comment-replies" },
          comment.replies.map((reply) => renderCommentNode(reply, onOpenPerson, onOpenLightbox))
        )
      : null;

  const extraPills = [];
  if (comment.like_count > 0) {
    extraPills.push(el("span", null, `♥ ${comment.like_count}`));
  }

  const inlineMedia = [];
  if (comment.stickers && comment.stickers.length > 0) {
    for (const st of comment.stickers) {
      if (typeof st === "string" && st.startsWith("media/")) {
        const sUrl = mediaUrl(st);
        if (sUrl) {
          inlineMedia.push(
            el("img", { src: sUrl, alt: "sticker", loading: "lazy", style: "max-height:72px;border-radius:8px;" })
          );
          continue;
        }
      }
      extraPills.push(el("span", { className: "pill" }, typeof st === "string" ? st : "스티커"));
    }
  }
  if (comment.media && comment.media.length > 0) {
    for (const med of comment.media) {
      const src = med && med.local_path ? mediaUrl(med.local_path) : null;
      if (!src) {
        extraPills.push(el("span", { className: "pill" }, "첨부 미디어"));
      } else if (isAudioMedia(med)) {
        inlineMedia.push(el("audio", { src, controls: true, preload: "metadata" }));
      } else if (isVideoMedia(med)) {
        inlineMedia.push(
          el("video", {
            src,
            controls: true,
            playsinline: true,
            preload: "metadata",
            style: "max-width:220px;border-radius:8px;",
          })
        );
      } else {
        inlineMedia.push(
          el("img", {
            src,
            alt: "comment media",
            loading: "lazy",
            style: "max-width:180px;max-height:180px;border-radius:8px;cursor:zoom-in;",
            onClick: () => onOpenLightbox && onOpenLightbox([med], 0),
          })
        );
      }
    }
  }

  const authorLabel = comment.author_name || comment.author_username || "?";

  return el(
    "div",
    { className: "comment-item" },
    avatarEl(comment.author_avatar_local_path, authorLabel, "sm"),
    el(
      "div",
      { className: "comment-content" },
      el(
        "div",
        { className: "comment-bubble" },
        el("div", { className: "comment-head" }, personButton(comment.author_username, comment.author_name, onOpenPerson)),
        comment.text ? el("div", { className: "comment-body" }, comment.text) : null,
        inlineMedia.length > 0
          ? el("div", { style: "display:flex;gap:8px;flex-wrap:wrap;margin-top:6px;" }, inlineMedia)
          : !comment.text
            ? el("div", { className: "comment-body", style: "color:var(--text-muted);" }, "(내용 없음)")
            : null
      ),
      el(
        "div",
        { className: "comment-submeta" },
        el("span", null, formatDate(comment.created_at, comment.timestamp_unix)),
        extraPills
      ),
      repliesNode
    )
  );
}

export function renderCommentTree(comments, onOpenPerson, onOpenLightbox = null) {
  if (!comments || comments.length === 0) {
    return el("div", { className: "empty-box", style: "padding:14px;font-size:0.86rem;" }, "저장된 댓글이 없습니다.");
  }
  return el(
    "div",
    { className: "comment-list" },
    comments.map((c) => renderCommentNode(c, onOpenPerson, onOpenLightbox))
  );
}
