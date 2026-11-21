/** Recursive nested comment/reply tree renderer. */

import { el, formatDate, personButton } from "../dom.js";

function renderCommentNode(comment, onOpenPerson) {
  const repliesNode =
    comment.replies && comment.replies.length > 0
      ? el(
          "div",
          { className: "comment-replies" },
          comment.replies.map((reply) => renderCommentNode(reply, onOpenPerson))
        )
      : null;

  const extraPills = [];
  if (comment.like_count > 0) {
    extraPills.push(el("span", { className: "pill" }, `♥ ${comment.like_count}`));
  }
  if (comment.stickers && comment.stickers.length > 0) {
    extraPills.push(el("span", { className: "pill" }, `스티커 ${comment.stickers.length}`));
  }
  if (comment.media && comment.media.length > 0) {
    extraPills.push(el("span", { className: "pill" }, `첨부 ${comment.media.length}`));
  }

  return el(
    "div",
    { className: "comment-item" },
    el(
      "div",
      { className: "comment-head" },
      personButton(comment.author_username, comment.author_name, onOpenPerson),
      el("span", { className: "pill" }, formatDate(comment.created_at, comment.timestamp_unix)),
      extraPills
    ),
    el("div", { className: "comment-body" }, comment.text || "(내용 없음)"),
    repliesNode
  );
}

export function renderCommentTree(comments, onOpenPerson) {
  if (!comments || comments.length === 0) {
    return el("div", { className: "empty-box", style: "padding:14px;" }, "저장된 댓글이 없습니다.");
  }
  return el(
    "div",
    { className: "comment-list" },
    comments.map((c) => renderCommentNode(c, onOpenPerson))
  );
}
