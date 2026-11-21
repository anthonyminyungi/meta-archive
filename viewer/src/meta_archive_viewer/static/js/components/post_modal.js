/** Full post detail modal showing carousel, metadata, likes list, and nested comments. */

import { api } from "../api.js";
import { clear, el, formatDate, personButton } from "../dom.js";
import { renderCarousel } from "./carousel.js";
import { renderCommentTree } from "./comments.js";

export function createPostModal(hostEl, { onOpenLightbox, onOpenPerson }) {
  function close() {
    clear(hostEl);
    hostEl.classList.add("hidden");
  }

  hostEl.addEventListener("click", (ev) => {
    if (ev.target === hostEl) close();
  });

  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !hostEl.classList.contains("hidden")) {
      close();
    }
  });

  async function open(postId) {
    clear(hostEl);
    const panel = el("div", { className: "modal-panel" }, el("div", { className: "loading-box" }, "불러오는 중..."));
    hostEl.appendChild(panel);
    hostEl.classList.remove("hidden");

    try {
      const post = await api.postDetail(postId);
      clear(panel);

      const header = el(
        "div",
        { className: "modal-header" },
        el(
          "div",
          null,
          el(
            "div",
            { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;" },
            el("span", { className: "pill pill-accent" }, post.platform.toUpperCase()),
            post.post_type ? el("span", { className: "pill" }, post.post_type) : null,
            post.is_archived ? el("span", { className: "pill pill-warn" }, "보관됨") : null,
            post.album_name ? el("span", { className: "pill" }, `앨범: ${post.album_name}`) : null
          ),
          el("small", { style: "color:var(--text-muted);" }, formatDate(post.created_at, post.timestamp_unix))
        ),
        el("button", { type: "button", className: "btn", onClick: close }, "닫기")
      );

      const authorRow = el(
        "div",
        { className: "post-meta-row", style: "margin-bottom:8px;" },
        el("strong", null, "작성자: "),
        personButton(post.author_username, post.author_name, (ident) => {
          close();
          onOpenPerson(ident);
        }),
        post.location && post.location.name ? el("span", { className: "pill" }, `📍 ${post.location.name}`) : null,
        post.audience ? el("span", { className: "pill" }, `공개: ${post.audience}`) : null
      );

      const carousel = renderCarousel(post.media, onOpenLightbox);
      const caption = post.caption ? el("div", { className: "post-caption" }, post.caption) : null;

      const hashtags =
        post.hashtags && post.hashtags.length > 0
          ? el(
              "div",
              { className: "post-meta-row" },
              post.hashtags.map((tag) => el("span", { className: "pill" }, `#${tag}`))
            )
          : null;

      const taggedUsers =
        post.tagged_users && post.tagged_users.length > 0
          ? el(
              "div",
              { className: "post-meta-row" },
              el("span", null, "태그된 사람:"),
              post.tagged_users.map((u) =>
                personButton(u.username, u.full_name, (ident) => {
                  close();
                  onOpenPerson(ident);
                })
              )
            )
          : null;

      let sharedBox = null;
      if (post.shared_post) {
        sharedBox = el(
          "div",
          { className: "shared-box" },
          post.shared_post.title ? el("div", null, el("strong", null, post.shared_post.title)) : null,
          post.shared_post.text ? el("div", null, post.shared_post.text) : null,
          post.shared_post.url ? el("div", { style: "word-break:break-all;" }, post.shared_post.url) : null
        );
      }

      const reactionsSummary = [];
      if (post.likes_count > 0 || (post.likes_users && post.likes_users.length > 0)) {
        reactionsSummary.push(
          el(
            "span",
            { className: "pill" },
            `좋아요 ${post.likes_count}${post.likes_is_truncated ? " (명단 상한 적용)" : ""}`
          )
        );
      }
      if (post.reactions && post.reactions.breakdown) {
        for (const [rtype, cnt] of Object.entries(post.reactions.breakdown)) {
          reactionsSummary.push(el("span", { className: "pill" }, `${rtype}: ${cnt}`));
        }
      }

      const likesBlock =
        post.likes_users && post.likes_users.length > 0
          ? el(
              "details",
              { style: "margin:10px 0;" },
              el("summary", { style: "cursor:pointer;font-weight:600;" }, `좋아요 누른 사람 (${post.likes_users.length}명)`),
              el(
                "div",
                { className: "post-meta-row", style: "margin-top:8px;" },
                post.likes_users.map((u) =>
                  personButton(u.username, u.full_name, (ident) => {
                    close();
                    onOpenPerson(ident);
                  })
                )
              )
            )
          : null;

      const commentsSection = el(
        "div",
        { style: "margin-top:14px;" },
        el("h3", { style: "margin:0 0 10px;font-size:1rem;" }, `댓글 (${post.comments_count || 0})`),
        renderCommentTree(post.comments, (ident) => {
          close();
          onOpenPerson(ident);
        })
      );

      panel.append(header, authorRow);
      if (carousel) panel.appendChild(carousel);
      if (caption) panel.appendChild(caption);
      if (sharedBox) panel.appendChild(sharedBox);
      if (hashtags) panel.appendChild(hashtags);
      if (taggedUsers) panel.appendChild(taggedUsers);
      if (reactionsSummary.length > 0) panel.appendChild(el("div", { className: "post-meta-row" }, reactionsSummary));
      if (likesBlock) panel.appendChild(likesBlock);
      panel.appendChild(commentsSection);
    } catch (err) {
      clear(panel);
      panel.appendChild(el("div", { className: "error-box" }, `게시물 로드 실패: ${err.message}`));
    }
  }

  async function openCustom(title, renderBody) {
    clear(hostEl);
    const panel = el(
      "div",
      { className: "modal-panel" },
      el(
        "div",
        { className: "modal-header" },
        el("h3", { style: "margin:0;" }, title),
        el("button", { type: "button", className: "btn", onClick: close }, "닫기")
      )
    );
    const body = el("div", null, el("div", { className: "loading-box" }, "불러오는 중..."));
    panel.appendChild(body);
    hostEl.appendChild(panel);
    hostEl.classList.remove("hidden");
    try {
      await renderBody(body, close);
    } catch (err) {
      clear(body);
      body.appendChild(el("div", { className: "error-box" }, `불러오기 실패: ${err.message}`));
    }
  }

  return { open, openCustom, close };
}
