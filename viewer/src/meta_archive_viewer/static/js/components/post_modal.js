/** Full post detail modal with split Instagram-style desktop layout, avatars, likes list, and nested comments. */

import { api } from "../api.js?v=2";
import {
  avatarEl,
  clear,
  el,
  extractReactionEntries,
  formatDate,
  formatReactionBadge,
  personButton,
  renderSharedCard,
} from "../dom.js?v=2";
import { renderCarousel } from "./carousel.js?v=2";
import { renderCommentTree } from "./comments.js?v=2";

export function createPostModal(hostEl, { onOpenLightbox, onOpenPerson }) {
  function close() {
    clear(hostEl);
    hostEl.classList.add("hidden");
  }

  hostEl.addEventListener("click", (ev) => {
    if (ev.target === hostEl) close();
  });

  async function open(postId) {
    clear(hostEl);
    const panel = el("div", { className: "modal-panel" }, el("div", { className: "loading-box" }, "불러오는 중..."));
    hostEl.appendChild(panel);
    hostEl.classList.remove("hidden");

    try {
      const post = await api.postDetail(postId);
      clear(panel);

      const carousel = renderCarousel(post.media, onOpenLightbox);
      const useSplit = Boolean(carousel);
      panel.className = useSplit ? "modal-panel modal-panel-split" : "modal-panel";

      const platCls = post.platform === "instagram" ? "pill-ig" : "pill-fb";
      const authorLabel = post.author_name || post.author_username || post.platform;

      const header = el(
        "div",
        { className: "modal-header" },
        el(
          "div",
          { className: "post-author-block" },
          avatarEl(post.author_avatar_local_path, authorLabel, "md"),
          el(
            "div",
            { className: "post-author-meta" },
            el(
              "div",
              { style: "display:flex;gap:6px;align-items:center;flex-wrap:wrap;" },
              personButton(post.author_username, post.author_name, (ident) => {
                close();
                onOpenPerson(ident);
              }),
              el("span", { className: `pill ${platCls}` }, post.platform.toUpperCase()),
              post.is_archived ? el("span", { className: "pill pill-warn" }, "보관됨") : null
            ),
            el(
              "div",
              { className: "post-author-sub" },
              el("span", null, formatDate(post.created_at, post.timestamp_unix)),
              post.location && post.location.name ? el("span", null, `· 📍 ${post.location.name}`) : null,
              post.album_name ? el("span", null, `· 📁 ${post.album_name}`) : null,
              post.group_name ? el("span", null, `· 👥 ${post.group_name}`) : null,
              post.audience ? el("span", null, `· ${post.audience}`) : null
            )
          )
        ),
        el("button", { type: "button", className: "btn btn-sm", onClick: close }, "✕ 닫기")
      );

      const activityTitle = post.activity_title
        ? el("div", { style: "font-size:0.84rem;color:var(--text-muted);margin-bottom:6px;" }, post.activity_title)
        : null;
      const caption = post.caption ? el("div", { className: "post-caption" }, post.caption) : null;

      const hashtags =
        post.hashtags && post.hashtags.length > 0
          ? el(
              "div",
              { className: "post-meta-row" },
              post.hashtags.map((tag) => el("span", { className: "pill pill-accent" }, `#${tag}`))
            )
          : null;

      const taggedUsers =
        post.tagged_users && post.tagged_users.length > 0
          ? el(
              "div",
              { className: "post-meta-row" },
              el("span", null, "🏷️ 태그:"),
              post.tagged_users.map((u) =>
                personButton(u.username, u.full_name, (ident) => {
                  close();
                  onOpenPerson(ident);
                })
              )
            )
          : null;

      const sharedBox = renderSharedCard(post.shared_post, onOpenLightbox);

      const reactionsSummary = [];
      if (post.likes_count > 0 || (post.likes_users && post.likes_users.length > 0)) {
        reactionsSummary.push(
          el(
            "span",
            { className: "pill pill-accent" },
            `♥ 좋아요 ${post.likes_count}${post.likes_is_truncated ? " (명단 상한)" : ""}`
          )
        );
      }
      for (const [rtype, cnt] of extractReactionEntries(post.reactions)) {
        reactionsSummary.push(el("span", { className: "pill" }, formatReactionBadge(rtype, cnt)));
      }

      const likesBlock =
        post.likes_users && post.likes_users.length > 0
          ? el(
              "details",
              { style: "margin:10px 0;" },
              el("summary", { style: "cursor:pointer;font-weight:600;font-size:0.88rem;" }, `좋아요 누른 사람 (${post.likes_users.length}명)`),
              el(
                "div",
                { className: "likes-avatar-list" },
                post.likes_users.map((u) =>
                  el(
                    "div",
                    { className: "likes-avatar-item" },
                    avatarEl(u.avatar_local_path, u.full_name || u.username || "?", "sm"),
                    personButton(u.username, u.full_name, (ident) => {
                      close();
                      onOpenPerson(ident);
                    })
                  )
                )
              )
            )
          : null;

      const commentsSection = el(
        "div",
        { style: "margin-top:14px;padding-top:12px;border-top:1px solid var(--border);" },
        el("h3", { style: "margin:0 0 10px;font-size:0.95rem;" }, `댓글 (${post.comments_count || 0})`),
        renderCommentTree(
          post.comments,
          (ident) => {
            close();
            onOpenPerson(ident);
          },
          onOpenLightbox
        )
      );

      if (useSplit) {
        const mediaCol = el("div", { className: "modal-media-col" }, carousel);
        const infoCol = el("div", { className: "modal-info-col" }, header);
        if (activityTitle) infoCol.appendChild(activityTitle);
        if (caption) infoCol.appendChild(caption);
        if (sharedBox) infoCol.appendChild(sharedBox);
        if (hashtags) infoCol.appendChild(hashtags);
        if (taggedUsers) infoCol.appendChild(taggedUsers);
        if (reactionsSummary.length > 0) infoCol.appendChild(el("div", { className: "post-meta-row" }, reactionsSummary));
        if (likesBlock) infoCol.appendChild(likesBlock);
        infoCol.appendChild(commentsSection);
        panel.append(mediaCol, infoCol);
      } else {
        panel.appendChild(header);
        if (activityTitle) panel.appendChild(activityTitle);
        if (caption) panel.appendChild(caption);
        if (sharedBox) panel.appendChild(sharedBox);
        if (hashtags) panel.appendChild(hashtags);
        if (taggedUsers) panel.appendChild(taggedUsers);
        if (reactionsSummary.length > 0) panel.appendChild(el("div", { className: "post-meta-row" }, reactionsSummary));
        if (likesBlock) panel.appendChild(likesBlock);
        panel.appendChild(commentsSection);
      }
    } catch (err) {
      clear(panel);
      panel.className = "modal-panel";
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
        el("h3", { style: "margin:0;font-size:1.05rem;" }, title),
        el("button", { type: "button", className: "btn btn-sm", onClick: close }, "✕ 닫기")
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
