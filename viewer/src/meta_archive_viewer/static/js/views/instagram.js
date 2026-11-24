/** Instagram archive view: clean profile, highlights, borderless 3-col hover grid with prefetch, feed cards, stories. */

import { api, mediaUrl } from "../api.js?v=3";
import {
  avatarEl,
  clear,
  el,
  formatDate,
  formatShortDate,
  isVideoMedia,
  personButton,
  renderCollapsibleText,
  safeExternalHref,
  skeletonCards,
  skeletonGrid,
} from "../dom.js?v=3";
import { icon } from "../icons.js?v=3";
import { renderCarousel } from "../components/carousel.js?v=3";

export async function renderInstagramView(container, ctx) {
  clear(container);
  container.appendChild(skeletonGrid(9));

  const state = {
    viewMode: "grid", // grid | feed
    archived: null, // null | true | false
    postType: null, // null | "tagged"
    offset: 0,
    limit: 30,
    total: 0,
    items: [],
  };

  const [profilesRes, highlightsRes, overview] = await Promise.all([
    api.profiles("instagram"),
    api.highlights("instagram"),
    api.overview(),
  ]);

  const profile = profilesRes.items[0] || null;
  const igStats = (overview.by_platform && overview.by_platform.instagram) || {};

  clear(container);
  const root = el("div", { className: "ig-root" });
  container.appendChild(root);

  function openConnectionsModal(relation) {
    const title = relation === "follower" ? "팔로워" : relation === "following" ? "팔로잉" : "전체 연결";
    ctx.modal.openCustom(`Instagram ${title}`, async (body, closeModal) => {
      let filterQ = "";
      clear(body);
      const searchInput = el("input", {
        type: "search",
        className: "input",
        placeholder: "아이디 또는 이름 검색...",
      });
      const listEl = el("div", { className: "connections-grid", style: "margin-top:12px;" });
      body.append(el("div", { className: "search-bar" }, searchInput), listEl);

      async function reload() {
        clear(listEl);
        listEl.appendChild(el("div", { className: "loading-box" }, "검색 중..."));
        const res = await api.connections({
          platform: "instagram",
          relation: relation || undefined,
          q: filterQ || undefined,
          limit: 200,
        });
        clear(listEl);
        if (res.items.length === 0) {
          listEl.appendChild(el("div", { className: "empty-box" }, "해당하는 계정이 없습니다."));
          return;
        }
        for (const c of res.items) {
          listEl.appendChild(
            el(
              "div",
              { className: "person-row" },
              avatarEl(c.avatar_local_path, c.username || c.full_name || "?", "md"),
              el(
                "div",
                { style: "min-width:0;flex:1;" },
                personButton(c.username, c.full_name, (ident) => {
                  closeModal();
                  ctx.openPerson(ident);
                }),
                el(
                  "div",
                  { className: "tabular", style: "font-size:0.76rem;color:var(--text-muted);" },
                  `${c.relation}${c.connected_at ? ` · ${formatShortDate(c.connected_at, c.timestamp_unix)}` : ""}`
                )
              )
            )
          );
        }
      }

      let timer = null;
      searchInput.addEventListener("input", () => {
        filterQ = searchInput.value;
        clearTimeout(timer);
        timer = setTimeout(reload, 180);
      });
      await reload();
    });
  }

  function openStoriesModal(highlightId = null, title = "전체 스토리") {
    ctx.modal.openCustom(`스토리 — ${title}`, async (body) => {
      const res = await api.stories({ platform: "instagram", highlight_id: highlightId, limit: 200 });
      clear(body);
      if (res.items.length === 0) {
        body.appendChild(el("div", { className: "empty-box" }, "저장된 스토리가 없습니다."));
        return;
      }
      const allMedia = res.items.map((s) => s.media).filter((m) => m && m.local_path);
      const grid = el("div", { className: "story-grid" });
      for (const s of res.items) {
        const src = s.media ? mediaUrl(s.media.local_path) : null;
        const lbIdx = s.media ? allMedia.indexOf(s.media) : -1;
        let mediaEl = null;
        if (src) {
          mediaEl = isVideoMedia(s.media)
            ? el("video", { src, controls: true, playsinline: true, preload: "metadata" })
            : el("img", {
                src,
                alt: s.highlight_title || "story",
                loading: "lazy",
                decoding: "async",
                style: "cursor:zoom-in;",
                onClick: () => ctx.openLightbox(allMedia, Math.max(0, lbIdx)),
              });
        }
        grid.appendChild(
          el(
            "div",
            { className: "card story-card" },
            el(
              "div",
              { className: "post-meta-row tabular", style: "margin-top:0;font-size:0.75rem;" },
              el("span", { className: "meta-chip" }, formatDate(s.created_at, s.timestamp_unix)),
              s.highlight_title ? el("span", { className: "meta-chip" }, s.highlight_title) : null,
              s.location_name ? el("span", { className: "inline-meta" }, icon("pin", 12), s.location_name) : null,
              s.likes_count > 0 ? el("span", { className: "inline-meta" }, icon("heart", 12), String(s.likes_count)) : null
            ),
            mediaEl,
            s.caption ? el("div", { style: "font-size:0.84rem;white-space:pre-wrap;" }, s.caption) : null
          )
        );
      }
      body.appendChild(grid);
    });
  }

  function openActivitiesModal() {
    ctx.modal.openCustom("Instagram 내 활동", async (body) => {
      let kind = "";
      let q = "";
      clear(body);
      const kindSelect = el(
        "select",
        { className: "select", "aria-label": "활동 종류" },
        el("option", { value: "" }, "전체 활동"),
        el("option", { value: "own_comment" }, "내가 쓴 댓글"),
        el("option", { value: "saved_item" }, "저장한 게시물"),
        el("option", { value: "repost" }, "리포스트")
      );
      const qInput = el("input", { type: "search", className: "input", placeholder: "댓글·저장 내용 또는 계정 검색..." });
      const listEl = el("div", { className: "feed-list", style: "margin-top:12px;" });
      body.append(el("div", { className: "search-bar" }, kindSelect, qInput), listEl);

      async function reload() {
        clear(listEl);
        listEl.appendChild(skeletonCards(2));
        const res = await api.activities({
          platform: "instagram",
          kind: kind || undefined,
          q: q || undefined,
          limit: 200,
        });
        clear(listEl);
        if (res.items.length === 0) {
          listEl.appendChild(el("div", { className: "empty-box" }, "해당하는 활동 내역이 없습니다."));
          return;
        }
        for (const a of res.items) {
          const href = safeExternalHref(a.url);
          listEl.appendChild(
            el(
              "div",
              { className: "card" },
              el(
                "div",
                { className: "post-meta-row tabular", style: "margin-top:0;margin-bottom:6px;" },
                el("span", { className: "meta-chip" }, a.kind),
                el("span", null, formatDate(a.created_at, a.timestamp_unix)),
                a.target_author ? el("span", { className: "meta-chip" }, `@${a.target_author}`) : null
              ),
              a.text ? el("div", { className: "post-caption" }, a.text) : null,
              href ? el("a", { href, target: "_blank", rel: "noopener noreferrer", style: "font-size:0.82rem;word-break:break-all;" }, a.url) : null
            )
          );
        }
      }

      let timer = null;
      kindSelect.addEventListener("change", () => {
        kind = kindSelect.value;
        reload();
      });
      qInput.addEventListener("input", () => {
        q = qInput.value;
        clearTimeout(timer);
        timer = setTimeout(reload, 180);
      });
      await reload();
    });
  }

  // Profile Header
  const displayName = profile ? profile.full_name || profile.username || "Instagram" : "Instagram Archive";
  const handleText = profile && profile.username ? `@${profile.username}` : null;

  root.appendChild(
    el(
      "section",
      { className: "profile-header" },
      el("div", { className: "avatar-ring-ig" }, avatarEl(profile ? profile.avatar_local_path : null, displayName, "lg")),
      el(
        "div",
        { className: "profile-meta" },
        el(
          "div",
          { className: "profile-title-row" },
          el("h1", null, displayName),
          handleText ? el("span", { className: "profile-handle" }, handleText) : null
        ),
        el(
          "div",
          { className: "profile-stats tabular" },
          el("span", { className: "profile-stat" }, "게시물", el("strong", null, (igStats.posts || 0).toLocaleString())),
          el(
            "button",
            { type: "button", className: "profile-stat-btn", onClick: () => openConnectionsModal("follower") },
            "팔로워",
            el("strong", null, String((profile && profile.counts && profile.counts.followers) ?? "목록"))
          ),
          el(
            "button",
            { type: "button", className: "profile-stat-btn", onClick: () => openConnectionsModal("following") },
            "팔로잉",
            el("strong", null, String((profile && profile.counts && profile.counts.following) ?? "목록"))
          ),
          el("span", { className: "profile-stat" }, "스토리", el("strong", null, (igStats.stories || 0).toLocaleString())),
          el("span", { className: "profile-stat" }, "댓글", el("strong", null, (igStats.comments || 0).toLocaleString())),
          (igStats.activities || 0) > 0
            ? el(
                "button",
                { type: "button", className: "profile-stat-btn", onClick: openActivitiesModal },
                "내 활동",
                el("strong", null, (igStats.activities || 0).toLocaleString())
              )
            : null
        ),
        profile && profile.bio ? el("p", { className: "profile-bio" }, profile.bio) : null
      )
    )
  );

  // Highlights & Stories Bar
  if (highlightsRes.items.length > 0 || (igStats.stories || 0) > 0) {
    const strip = el("section", { className: "highlights-strip" });
    strip.appendChild(
      el(
        "button",
        {
          type: "button",
          className: "highlight-btn",
          onClick: () => openStoriesModal(null, "전체 스토리"),
        },
        el("span", { className: "highlight-ring" }, el("span", { className: "highlight-ring-inner" }, el("strong", { style: "font-size:0.74rem;letter-spacing:0.04em;" }, "ALL"))),
        el("span", { className: "highlight-label tabular" }, `스토리 ${igStats.stories || 0}`)
      )
    );
    for (const h of highlightsRes.items) {
      const coverSrc = mediaUrl(h.highlight_cover);
      strip.appendChild(
        el(
          "button",
          {
            type: "button",
            className: "highlight-btn",
            onClick: () => openStoriesModal(h.highlight_id, h.highlight_title),
          },
          el(
            "span",
            { className: "highlight-ring" },
            el(
              "span",
              { className: "highlight-ring-inner" },
              coverSrc
                ? el("img", { src: coverSrc, alt: h.highlight_title, loading: "lazy", decoding: "async" })
                : el("strong", null, (h.highlight_title || "HL").slice(0, 2))
            )
          ),
          el("span", { className: "highlight-label", title: `${h.highlight_title} (${h.count})` }, h.highlight_title)
        )
      );
    }
    root.appendChild(strip);
  }

  // Toolbar
  const filterSelect = el(
    "select",
    { className: "select", "aria-label": "보관 상태 필터" },
    el("option", { value: "all" }, "전체 게시물"),
    el("option", { value: "active" }, "활성 게시물"),
    el("option", { value: "archived" }, "보관됨 (Archived)"),
    el("option", { value: "tagged" }, "태그된 게시물 (Tagged)")
  );
  const gridBtn = el("button", { type: "button", className: "seg-btn active" }, icon("grid", 14), "그리드");
  const feedBtn = el("button", { type: "button", className: "seg-btn" }, icon("list", 14), "피드");
  const countLabel = el("span", { className: "toolbar-count tabular" }, "");

  const postsHost = el("div");
  const loadMoreWrap = el("div", { className: "load-more-wrap" });
  const loadMoreBtn = el("button", { type: "button", className: "btn" }, "더 보기");
  loadMoreWrap.appendChild(loadMoreBtn);

  root.appendChild(
    el(
      "div",
      { className: "toolbar" },
      el("div", { className: "toolbar-group" }, filterSelect, countLabel),
      el("div", { className: "segmented-control" }, gridBtn, feedBtn)
    )
  );
  root.appendChild(postsHost);
  root.appendChild(loadMoreWrap);

  function renderPosts() {
    clear(postsHost);
    countLabel.textContent = `${state.items.length.toLocaleString()} / ${state.total.toLocaleString()}`;
    loadMoreWrap.classList.toggle("hidden", state.items.length >= state.total);

    gridBtn.classList.toggle("active", state.viewMode === "grid");
    feedBtn.classList.toggle("active", state.viewMode === "feed");

    if (state.items.length === 0) {
      postsHost.appendChild(el("div", { className: "empty-box" }, "표시할 Instagram 게시물이 없습니다."));
      return;
    }

    if (state.viewMode === "grid") {
      const grid = el("div", { className: "ig-grid" });
      for (const post of state.items) {
        const primary = (post.media || []).find((m) => m.local_path) || null;
        const src = primary ? mediaUrl(primary.local_path) : null;
        let thumb;
        if (src && isVideoMedia(primary)) {
          thumb = el("video", { src, muted: true, preload: "metadata" });
        } else if (src) {
          thumb = el("img", {
            src,
            alt: post.caption ? post.caption.slice(0, 40) : post.post_id,
            loading: "lazy",
            decoding: "async",
          });
        } else {
          thumb = el("div", { className: "ig-tile-text-only" }, (post.caption || post.post_id).slice(0, 60));
        }

        const rightBadge =
          (post.media || []).length > 1
            ? el("span", { className: "tile-badge tabular" }, icon("layers", 12), String(post.media.length))
            : isVideoMedia(primary)
              ? el("span", { className: "tile-badge" }, icon("play", 11))
              : null;

        grid.appendChild(
          el(
            "button",
            {
              type: "button",
              className: "ig-tile",
              onPointerenter: () => api.prefetchPost(post.post_id),
              onClick: () => ctx.openPost(post.post_id),
            },
            thumb,
            el(
              "div",
              { className: "ig-tile-top-badges" },
              post.is_archived ? el("span", { className: "tile-badge" }, "보관") : el("span"),
              rightBadge
            ),
            el(
              "div",
              { className: "ig-tile-hover tabular" },
              el(
                "div",
                { className: "ig-tile-hover-metrics" },
                el("span", { className: "inline-meta" }, icon("heartFilled", 14), String(post.likes_count || 0)),
                el("span", { className: "inline-meta" }, icon("comment", 14), String(post.comments_count || 0))
              ),
              el("small", null, formatShortDate(post.created_at, post.timestamp_unix))
            )
          )
        );
      }
      postsHost.appendChild(grid);
      return;
    }

    const feed = el("div", { className: "feed-list" });
    for (const post of state.items) {
      const carousel = renderCarousel(post.media, ctx.openLightbox);
      const authorLabel = post.author_name || post.author_username || "Instagram";
      feed.appendChild(
        el(
          "article",
          { className: "card feed-card" },
          el(
            "div",
            { className: "post-card-head" },
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
                  personButton(post.author_username, post.author_name, ctx.openPerson),
                  post.is_archived ? el("span", { className: "pill pill-warn" }, "보관됨") : null
                ),
                el(
                  "div",
                  { className: "post-author-sub tabular" },
                  el("span", null, formatDate(post.created_at, post.timestamp_unix)),
                  post.location && post.location.name
                    ? el("span", { className: "inline-meta" }, icon("pin", 12), post.location.name)
                    : null
                )
              )
            )
          ),
          carousel,
          post.caption ? renderCollapsibleText(post.caption, { maxChars: 280, maxLines: 5 }) : null,
          el(
            "div",
            { className: "post-footer-bar" },
            el("span", { className: "stat-chip tabular" }, icon("heart", 13), `좋아요 ${(post.likes_count || 0).toLocaleString()}`),
            el(
              "button",
              { type: "button", className: "btn btn-sm btn-ghost tabular", onClick: () => ctx.openPost(post.post_id) },
              icon("comment", 14),
              `댓글 ${post.comments_count || 0}`
            )
          )
        )
      );
    }
    postsHost.appendChild(feed);
  }

  async function fetchPage(reset = false) {
    if (reset) {
      state.offset = 0;
      state.items = [];
    }
    loadMoreBtn.disabled = true;
    const res = await api.posts({
      platform: "instagram",
      offset: state.offset,
      limit: state.limit,
      archived: state.archived,
      post_type: state.postType || undefined,
    });
    state.total = res.total;
    state.items.push(...res.items);
    state.offset += res.items.length;
    loadMoreBtn.disabled = false;
    renderPosts();
  }

  filterSelect.addEventListener("change", () => {
    const val = filterSelect.value;
    state.postType = val === "tagged" ? "tagged" : null;
    state.archived = val === "archived" ? true : val === "active" ? false : null;
    fetchPage(true);
  });
  gridBtn.addEventListener("click", () => {
    state.viewMode = "grid";
    renderPosts();
  });
  feedBtn.addEventListener("click", () => {
    state.viewMode = "feed";
    renderPosts();
  });
  loadMoreBtn.addEventListener("click", () => fetchPage(false));

  await fetchPage(true);
}
