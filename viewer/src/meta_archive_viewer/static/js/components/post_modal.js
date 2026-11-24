/** Full post detail modal with fixed-height split layout, keyboard carousel navigation, avatars, and nested comments. */

import { api } from "../api.js?v=3";
import {
  avatarEl,
  clear,
  el,
  extractReactionEntries,
  formatDate,
  formatReactionBadge,
  personButton,
  reactionLabel,
  renderSharedCard,
  skeletonCards,
} from "../dom.js?v=3";
import { icon } from "../icons.js?v=3";
import { renderCarousel } from "./carousel.js?v=3";
import { renderCommentTree } from "./comments.js?v=3";

export function createPostModal(hostEl, { onOpenLightbox, onOpenPerson, isLightboxOpen = () => false }) {
  let activeCarouselStep = null;

  function close() {
    activeCarouselStep = null;
    clear(hostEl);
    hostEl.classList.add("hidden");
  }

  hostEl.addEventListener("click", (ev) => {
    if (ev.target === hostEl) close();
  });

  window.addEventListener("keydown", (ev) => {
    if (hostEl.classList.contains("hidden") || !activeCarouselStep || isLightboxOpen()) return;
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (ev.key === "ArrowLeft") {
      ev.preventDefault();
      activeCarouselStep(-1);
    } else if (ev.key === "ArrowRight") {
      ev.preventDefault();
      activeCarouselStep(1);
    }
  });

  async function open(postId) {
    activeCarouselStep = null;
    clear(hostEl);
    const panel = el("div", { className: "modal-panel" }, skeletonCards(2));
    hostEl.appendChild(panel);
    hostEl.classList.remove("hidden");

    try {
      const post = await api.postDetail(postId);
      clear(panel);

      const carousel = renderCarousel(post.media, onOpenLightbox, { eager: true });
      if (carousel && typeof carousel._carouselStep === "function") {
        activeCarouselStep = carousel._carouselStep;
      }
      const useSplit = Boolean(carousel);
      panel.className = useSplit ? "modal-panel modal-panel-split" : "modal-panel";

      const authorLabel = post.author_name || post.author_username || post.platform;

      const subItems = [el("span", null, formatDate(post.created_at, post.timestamp_unix))];
      if (post.location && post.location.name) {
        subItems.push(el("span", { className: "inline-meta" }, icon("pin", 12), post.location.name));
      }
      if (post.album_name) {
        subItems.push(el("span", { className: "inline-meta" }, icon("folder", 12), post.album_name));
      }
      if (post.group_name) {
        subItems.push(el("span", { className: "inline-meta" }, icon("users", 12), post.group_name));
      }
      if (post.audience) {
        subItems.push(el("span", null, post.audience));
      }

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
              { className: "author-name-line" },
              personButton(post.author_username, post.author_name, (ident) => {
                close();
                onOpenPerson(ident);
              }),
              el("span", { className: `plat-tag ${post.platform}` }, post.platform === "instagram" ? "IG" : "FB"),
              post.is_archived ? el("span", { className: "pill pill-warn" }, "보관됨") : null
            ),
            el("div", { className: "post-author-sub" }, subItems)
          )
        ),
        el(
          "button",
          { type: "button", className: "icon-btn", onClick: close, "aria-label": "닫기", title: "닫기 (ESC)" },
          icon("close", 17)
        )
      );

      const activityTitle = post.activity_title
        ? el("div", { className: "activity-subtitle" }, post.activity_title)
        : null;
      const caption = post.caption ? el("div", { className: "post-caption" }, post.caption) : null;

      const hashtags =
        post.hashtags && post.hashtags.length > 0
          ? el(
              "div",
              { className: "hashtag-row" },
              post.hashtags.map((tag) => el("span", { className: "hashtag-chip" }, `#${tag}`))
            )
          : null;

      const taggedUsers =
        post.tagged_users && post.tagged_users.length > 0
          ? el(
              "div",
              { className: "post-meta-row" },
              el("span", { className: "inline-meta" }, icon("tag", 13), "태그"),
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
            { className: "stat-chip" },
            icon("heart", 13),
            `좋아요 ${post.likes_count.toLocaleString()}${post.likes_is_truncated ? " (상한)" : ""}`
          )
        );
      }
      for (const [rtype, cnt] of extractReactionEntries(post.reactions)) {
        reactionsSummary.push(
          el("span", { className: "reaction-chip", title: reactionLabel(rtype) }, formatReactionBadge(rtype, cnt))
        );
      }

      const likesBlock =
        post.likes_users && post.likes_users.length > 0
          ? el(
              "details",
              { className: "likes-details" },
              el("summary", null, `좋아요 누른 사람 (${post.likes_users.length.toLocaleString()}명)`),
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
        { className: "modal-comments-section" },
        el("h3", { className: "section-heading" }, `댓글 ${post.comments_count || 0}`),
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
        const infoBody = el("div", { className: "modal-info-scroll" });
        if (activityTitle) infoBody.appendChild(activityTitle);
        if (caption) infoBody.appendChild(caption);
        if (sharedBox) infoBody.appendChild(sharedBox);
        if (hashtags) infoBody.appendChild(hashtags);
        if (taggedUsers) infoBody.appendChild(taggedUsers);
        if (reactionsSummary.length > 0) infoBody.appendChild(el("div", { className: "post-meta-row" }, reactionsSummary));
        if (likesBlock) infoBody.appendChild(likesBlock);
        infoBody.appendChild(commentsSection);
        const infoCol = el("div", { className: "modal-info-col" }, header, infoBody);
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
    activeCarouselStep = null;
    clear(hostEl);
    const panel = el(
      "div",
      { className: "modal-panel" },
      el(
        "div",
        { className: "modal-header" },
        el("h3", { className: "modal-title" }, title),
        el(
          "button",
          { type: "button", className: "icon-btn", onClick: close, "aria-label": "닫기", title: "닫기 (ESC)" },
          icon("close", 17)
        )
      )
    );
    const body = el("div", null, skeletonCards(2));
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
