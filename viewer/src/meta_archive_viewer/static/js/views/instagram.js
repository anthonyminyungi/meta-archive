/** Instagram archive view: profile header, connections modal, highlights/stories, 3-col grid & feed toggle. */

import { api, mediaUrl } from "../api.js";
import { avatarEl, clear, el, formatDate, isVideoMedia, personButton, safeExternalHref } from "../dom.js";
import { renderCarousel } from "../components/carousel.js";

export async function renderInstagramView(container, ctx) {
  clear(container);
  container.appendChild(el("div", { className: "loading-box" }, "Instagram 아카이브 불러오는 중..."));

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
              avatarEl(c.avatar_local_path, c.username || c.full_name || "?"),
              el(
                "div",
                { style: "min-width:0;flex:1;" },
                personButton(c.username, c.full_name, (ident) => {
                  closeModal();
                  ctx.openPerson(ident);
                }),
                el(
                  "div",
                  { style: "font-size:0.78rem;color:var(--text-muted);" },
                  `${c.relation}${c.connected_at ? ` · ${formatDate(c.connected_at, c.timestamp_unix)}` : ""}`
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
      const list = el("div", { className: "feed-list" });
      for (const s of res.items) {
        const src = s.media ? mediaUrl(s.media.local_path) : null;
        let mediaEl = null;
        if (src) {
          mediaEl = isVideoMedia(s.media)
            ? el("video", { src, controls: true, playsinline: true, preload: "metadata", style: "max-width:100%;max-height:420px;border-radius:8px;" })
            : el("img", {
                src,
                alt: s.highlight_title || "story",
                loading: "lazy",
                style: "max-width:100%;max-height:420px;border-radius:8px;cursor:zoom-in;",
                onClick: () => ctx.openLightbox([s.media], 0),
              });
        }
        list.appendChild(
          el(
            "div",
            { className: "card" },
            el(
              "div",
              { className: "post-meta-row", style: "margin-top:0;margin-bottom:8px;" },
              el("span", { className: "pill pill-accent" }, formatDate(s.created_at, s.timestamp_unix)),
              s.highlight_title ? el("span", { className: "pill" }, s.highlight_title) : null,
              s.location_name ? el("span", { className: "pill" }, `📍 ${s.location_name}`) : null,
              s.likes_count > 0 ? el("span", { className: "pill" }, `♥ ${s.likes_count}`) : null
            ),
            mediaEl,
            s.caption ? el("div", { className: "post-caption" }, s.caption) : null,
            s.stickers && s.stickers.length > 0
              ? el("div", { className: "post-meta-row" }, s.stickers.map((st) => el("span", { className: "pill" }, st)))
              : null
          )
        );
      }
      body.appendChild(list);
    });
  }

  function openActivitiesModal() {
    ctx.modal.openCustom("Instagram 내 활동 (내 댓글 · 저장됨 · 리포스트)", async (body) => {
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
        listEl.appendChild(el("div", { className: "loading-box" }, "불러오는 중..."));
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
                { className: "post-meta-row", style: "margin-top:0;margin-bottom:6px;" },
                el("span", { className: "pill pill-accent" }, a.kind),
                el("span", { className: "pill" }, formatDate(a.created_at, a.timestamp_unix)),
                a.target_author ? el("span", { className: "pill" }, `@${a.target_author}`) : null
              ),
              a.text ? el("div", { className: "post-caption" }, a.text) : null,
              href ? el("a", { href, target: "_blank", rel: "noopener noreferrer" }, a.url) : null
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
  const displayTitle = profile
    ? `${profile.full_name || profile.username || "Instagram"}${profile.username ? ` (@${profile.username})` : ""}`
    : "Instagram Archive";

  root.appendChild(
    el(
      "section",
      { className: "card profile-header" },
      avatarEl(profile ? profile.avatar_local_path : null, displayTitle, true),
      el(
        "div",
        { className: "profile-meta" },
        el(
          "div",
          { className: "profile-title-row" },
          el("h1", null, displayTitle),
          el("span", { className: "pill pill-accent" }, `스토리 ${igStats.stories || 0}`),
          el("span", { className: "pill" }, `댓글 ${igStats.comments || 0}`)
        ),
        el(
          "div",
          { className: "profile-stats" },
          el("span", null, `게시물 ${igStats.posts || 0}`),
          el(
            "button",
            { type: "button", className: "btn-link", onClick: () => openConnectionsModal("follower") },
            `팔로워 ${(profile && profile.counts && profile.counts.followers) ?? "목록"}`
          ),
          el(
            "button",
            { type: "button", className: "btn-link", onClick: () => openConnectionsModal("following") },
            `팔로잉 ${(profile && profile.counts && profile.counts.following) ?? "목록"}`
          ),
          (igStats.activities || 0) > 0
            ? el(
                "button",
                { type: "button", className: "btn-link", onClick: openActivitiesModal },
                `내 댓글·저장됨 (${igStats.activities})`
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
        el("span", { className: "highlight-ring" }, el("strong", null, "ALL")),
        el("span", { className: "highlight-label" }, `스토리 (${igStats.stories || 0})`)
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
            coverSrc
              ? el("img", { src: coverSrc, alt: h.highlight_title, loading: "lazy" })
              : el("strong", null, (h.highlight_title || "HL").slice(0, 2))
          ),
          el("span", { className: "highlight-label", title: h.highlight_title }, `${h.highlight_title} (${h.count})`)
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
  const gridBtn = el("button", { type: "button", className: "btn btn-primary" }, "3열 그리드");
  const feedBtn = el("button", { type: "button", className: "btn" }, "피드 보기");
  const countPill = el("span", { className: "pill" }, "0건");

  const postsHost = el("div");
  const loadMoreWrap = el("div", { className: "load-more-wrap" });
  const loadMoreBtn = el("button", { type: "button", className: "btn" }, "더 보기");
  loadMoreWrap.appendChild(loadMoreBtn);

  root.appendChild(
    el(
      "div",
      { className: "toolbar" },
      el("div", { className: "toolbar-group" }, filterSelect, countPill),
      el("div", { className: "toolbar-group" }, gridBtn, feedBtn)
    )
  );
  root.appendChild(postsHost);
  root.appendChild(loadMoreWrap);

  function renderPosts() {
    clear(postsHost);
    countPill.textContent = `${state.items.length} / ${state.total}건`;
    loadMoreWrap.classList.toggle("hidden", state.items.length >= state.total);

    gridBtn.className = `btn${state.viewMode === "grid" ? " btn-primary" : ""}`;
    feedBtn.className = `btn${state.viewMode === "feed" ? " btn-primary" : ""}`;

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
          thumb = el("img", { src, alt: post.caption ? post.caption.slice(0, 40) : post.post_id, loading: "lazy" });
        } else {
          thumb = el("div", { className: "ig-tile-text-only" }, (post.caption || post.post_id).slice(0, 60));
        }

        const badge =
          (post.media || []).length > 1
            ? el("span", { className: "tile-badge" }, `❐ ${post.media.length}`)
            : isVideoMedia(primary)
              ? el("span", { className: "tile-badge" }, "▶ 영상")
              : null;

        grid.appendChild(
          el(
            "button",
            {
              type: "button",
              className: "ig-tile",
              onClick: () => ctx.openPost(post.post_id),
            },
            thumb,
            el(
              "div",
              { className: "ig-tile-overlay" },
              post.is_archived ? el("span", { className: "tile-badge" }, "보관") : el("span"),
              badge || el("span", { className: "tile-badge" }, `♥ ${post.likes_count} · 💬 ${post.comments_count}`)
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
      feed.appendChild(
        el(
          "article",
          { className: "card" },
          el(
            "div",
            { className: "post-card-head" },
            el(
              "div",
              { className: "post-author-block" },
              personButton(post.author_username, post.author_name, ctx.openPerson),
              post.is_archived ? el("span", { className: "pill pill-warn" }, "보관됨") : null,
              post.location && post.location.name ? el("span", { className: "pill" }, `📍 ${post.location.name}`) : null
            ),
            el("span", { style: "font-size:0.82rem;color:var(--text-muted);" }, formatDate(post.created_at, post.timestamp_unix))
          ),
          carousel,
          post.caption ? el("div", { className: "post-caption" }, post.caption) : null,
          el(
            "div",
            { className: "post-meta-row" },
            el("span", { className: "pill" }, `좋아요 ${post.likes_count}`),
            el(
              "button",
              { type: "button", className: "btn", onClick: () => ctx.openPost(post.post_id) },
              `상세 & 댓글 ${post.comments_count} 보기`
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
