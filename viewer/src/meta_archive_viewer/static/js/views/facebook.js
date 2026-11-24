/** Facebook archive view: year-accordion sidebar, editorial feed cards, OG banners, albums, friends, activities. */

import { api, mediaUrl } from "../api.js?v=3";
import {
  avatarEl,
  clear,
  el,
  extractReactionEntries,
  formatDate,
  formatReactionBadge,
  formatShortDate,
  isVideoMedia,
  personButton,
  reactionLabel,
  renderCollapsibleText,
  renderSharedCard,
  safeExternalHref,
  skeletonCards,
} from "../dom.js?v=3";
import { icon } from "../icons.js?v=3";
import { renderCarousel } from "../components/carousel.js?v=3";

export async function renderFacebookView(container, ctx) {
  clear(container);
  container.appendChild(skeletonCards(3));

  const [profilesRes, timelineRes, albumsRes, overview] = await Promise.all([
    api.profiles("facebook"),
    api.timeline("facebook"),
    api.albums("facebook"),
    api.overview(),
  ]);

  const profile = profilesRes.items[0] || null;
  const fbStats = (overview.by_platform && overview.by_platform.facebook) || {};

  const yearGroups = [];
  const byYearMap = new Map();
  for (const ym of timelineRes.items) {
    let grp = byYearMap.get(ym.year);
    if (!grp) {
      grp = { year: ym.year, total: 0, months: [] };
      byYearMap.set(ym.year, grp);
      yearGroups.push(grp);
    }
    grp.total += ym.count;
    grp.months.push(ym);
  }

  const state = {
    subtab: "timeline", // timeline | albums | friends | activities
    year: null,
    month: null,
    expandedYear: yearGroups[0] ? yearGroups[0].year : null,
    album: null,
    postTypeFilter: "",
    offset: 0,
    limit: 25,
    total: 0,
    items: [],
  };

  clear(container);
  const root = el("div");
  container.appendChild(root);

  const displayName = profile ? profile.full_name || profile.username || "Facebook" : "Facebook Archive";
  const tabTimelineBtn = el("button", { type: "button", className: "seg-btn active" }, "타임라인");
  const tabAlbumsBtn = el("button", { type: "button", className: "seg-btn tabular" }, `앨범 ${albumsRes.items.length}`);
  const tabFriendsBtn = el("button", { type: "button", className: "seg-btn tabular" }, `친구·팔로잉 ${(fbStats.connections || 0).toLocaleString()}`);
  const tabActivitiesBtn = el("button", { type: "button", className: "seg-btn tabular" }, `내 활동 ${(fbStats.activities || 0).toLocaleString()}`);

  function openFbStoriesModal() {
    ctx.modal.openCustom("Facebook 보관 스토리", async (body) => {
      const res = await api.stories({ platform: "facebook", limit: 100 });
      clear(body);
      if (res.items.length === 0) {
        body.appendChild(el("div", { className: "empty-box" }, "보관된 Facebook 스토리가 없습니다."));
        return;
      }
      const allMedia = res.items.map((s) => s.media).filter((m) => m && m.local_path);
      const grid = el("div", { className: "story-grid" });
      for (const s of res.items) {
        const src = s.media ? mediaUrl(s.media.local_path) : null;
        const lbIdx = s.media ? allMedia.indexOf(s.media) : -1;
        grid.appendChild(
          el(
            "div",
            { className: "card story-card" },
            el(
              "div",
              { className: "post-meta-row tabular", style: "margin-top:0;font-size:0.75rem;" },
              el("span", { className: "meta-chip" }, formatDate(s.created_at, s.timestamp_unix))
            ),
            src
              ? isVideoMedia(s.media)
                ? el("video", { src, controls: true, playsinline: true, preload: "metadata" })
                : el("img", {
                    src,
                    alt: "story",
                    loading: "lazy",
                    decoding: "async",
                    style: "cursor:zoom-in;",
                    onClick: () => ctx.openLightbox(allMedia, Math.max(0, lbIdx)),
                  })
              : null,
            s.caption ? el("div", { style: "font-size:0.84rem;white-space:pre-wrap;" }, s.caption) : null
          )
        );
      }
      body.appendChild(grid);
    });
  }

  root.appendChild(
    el(
      "section",
      { className: "profile-header" },
      avatarEl(profile ? profile.avatar_local_path : null, displayName, "lg"),
      el(
        "div",
        { className: "profile-meta" },
        el(
          "div",
          { className: "profile-title-row" },
          el("h1", null, displayName),
          profile && profile.username ? el("span", { className: "profile-handle" }, `@${profile.username}`) : null
        ),
        el(
          "div",
          { className: "profile-stats tabular" },
          el("span", { className: "profile-stat" }, "게시물", el("strong", null, (fbStats.posts || 0).toLocaleString())),
          el("span", { className: "profile-stat" }, "댓글", el("strong", null, (fbStats.comments || 0).toLocaleString())),
          (fbStats.stories || 0) > 0
            ? el(
                "button",
                { type: "button", className: "profile-stat-btn", onClick: openFbStoriesModal },
                "보관 스토리",
                el("strong", null, String(fbStats.stories))
              )
            : null
        ),
        profile && profile.bio ? el("p", { className: "profile-bio" }, profile.bio) : null,
        el(
          "div",
          { className: "segmented-control", style: "margin-top:14px;" },
          tabTimelineBtn,
          tabAlbumsBtn,
          tabFriendsBtn,
          tabActivitiesBtn
        )
      )
    )
  );

  const contentHost = el("div");
  root.appendChild(contentHost);

  function updateSubtabButtons() {
    tabTimelineBtn.classList.toggle("active", state.subtab === "timeline");
    tabAlbumsBtn.classList.toggle("active", state.subtab === "albums");
    tabFriendsBtn.classList.toggle("active", state.subtab === "friends");
    tabActivitiesBtn.classList.toggle("active", state.subtab === "activities");
  }

  async function renderTimeline() {
    clear(contentHost);
    const sidebar = el("aside", { className: "fb-sidebar" });
    const mainCol = el("div", { style: "min-width:0;" });
    const layout = el("div", { className: "fb-layout" }, sidebar, mainCol);
    contentHost.appendChild(layout);

    sidebar.appendChild(el("div", { className: "sidebar-heading" }, "타임라인"));
    const tmList = el("div", { className: "timeline-list" });
    sidebar.appendChild(tmList);

    const activeFilterRow = el("div", { className: "toolbar" });
    const feedHost = el("div", { className: "feed-list" });
    const moreWrap = el("div", { className: "load-more-wrap" });
    const moreBtn = el("button", { type: "button", className: "btn" }, "더 보기");
    moreWrap.appendChild(moreBtn);
    mainCol.append(activeFilterRow, feedHost, moreWrap);

    function renderSidebarButtons() {
      clear(tmList);
      tmList.appendChild(
        el(
          "button",
          {
            type: "button",
            className: `year-header-btn${!state.year && !state.month ? " active" : ""}`,
            onClick: () => {
              state.year = null;
              state.month = null;
              loadTimelinePosts(true);
            },
          },
          el("span", null, "전체 기간"),
          el("span", { className: "timeline-count tabular" }, (fbStats.posts || 0).toLocaleString())
        )
      );

      for (const grp of yearGroups) {
        const isYearSelected = state.year === grp.year && !state.month;
        const isExpanded = state.expandedYear === grp.year || state.year === grp.year;

        const yearHeader = el(
          "button",
          {
            type: "button",
            className: `year-header-btn${isYearSelected ? " active" : ""}`,
            onClick: () => {
              state.expandedYear = state.expandedYear === grp.year && state.year === grp.year ? null : grp.year;
              state.year = grp.year;
              state.month = null;
              loadTimelinePosts(true);
            },
          },
          el(
            "span",
            { className: "inline-meta tabular" },
            icon(isExpanded ? "chevronDown" : "chevronRight", 13),
            `${grp.year}`
          ),
          el("span", { className: "timeline-count tabular" }, String(grp.total))
        );

        const monthsWrap = isExpanded
          ? el(
              "div",
              { className: "year-months" },
              grp.months.map((ym) => {
                const isActive = state.year === ym.year && state.month === ym.month;
                return el(
                  "button",
                  {
                    type: "button",
                    className: `timeline-btn tabular${isActive ? " active" : ""}`,
                    onClick: () => {
                      state.year = ym.year;
                      state.month = ym.month;
                      state.expandedYear = ym.year;
                      loadTimelinePosts(true);
                    },
                  },
                  el("span", null, `${String(ym.month).padStart(2, "0")}월`),
                  el("span", { className: "timeline-count" }, String(ym.count))
                );
              })
            )
          : null;

        tmList.appendChild(el("div", { className: "year-group" }, yearHeader, monthsWrap));
      }
    }

    function renderPostList() {
      renderSidebarButtons();
      clear(activeFilterRow);
      const chips = [];
      if (state.year) chips.push(`${state.year}년${state.month ? ` ${state.month}월` : ""}`);
      if (state.album) chips.push(`앨범 · ${state.album}`);

      const typeButtons = [
        ["", "전체"],
        ["original", "내 글·사진"],
        ["group_post", "그룹"],
        ["shared", "공유"],
      ].map(([val, label]) =>
        el(
          "button",
          {
            type: "button",
            className: `seg-btn${state.postTypeFilter === val ? " active" : ""}`,
            onClick: () => {
              state.postTypeFilter = val;
              loadTimelinePosts(true);
            },
          },
          label
        )
      );

      activeFilterRow.appendChild(
        el(
          "div",
          { className: "toolbar-group" },
          el("span", { className: "active-scope-label" }, chips.length ? chips.join(" · ") : "전체 기간"),
          el("span", { className: "toolbar-count tabular" }, `${state.items.length.toLocaleString()} / ${state.total.toLocaleString()}`),
          chips.length
            ? el(
                "button",
                {
                  type: "button",
                  className: "btn btn-sm btn-ghost",
                  onClick: () => {
                    state.year = null;
                    state.month = null;
                    state.album = null;
                    loadTimelinePosts(true);
                  },
                },
                "초기화"
              )
            : null
        )
      );
      activeFilterRow.appendChild(el("div", { className: "segmented-control" }, typeButtons));

      clear(feedHost);
      moreWrap.classList.toggle("hidden", state.items.length >= state.total);
      if (state.items.length === 0) {
        feedHost.appendChild(el("div", { className: "empty-box" }, "해당 조건의 Facebook 게시물이 없습니다."));
        return;
      }

      for (const post of state.items) {
        const carousel = renderCarousel(post.media, ctx.openLightbox);
        const reactionChips = [];
        const entries = extractReactionEntries(post.reactions);
        if (entries.length > 0) {
          for (const [k, v] of entries) {
            reactionChips.push(
              el("span", { className: "reaction-chip tabular", title: reactionLabel(k) }, formatReactionBadge(k, v))
            );
          }
        } else if (post.reactions_total > 0) {
          reactionChips.push(el("span", { className: "reaction-chip tabular", title: "반응" }, `👍 ${post.reactions_total}`));
        }

        const sharedNode = renderSharedCard(post.shared_post, ctx.openLightbox);
        const authorLabel = post.author_name || post.author_username || displayName;

        const subParts = [el("span", null, formatDate(post.created_at, post.timestamp_unix))];
        if (post.album_name) {
          subParts.push(el("span", { className: "inline-meta" }, icon("folder", 12), post.album_name));
        }
        if (post.group_name) {
          subParts.push(el("span", { className: "inline-meta" }, icon("users", 12), post.group_name));
        }
        if (post.audience) {
          subParts.push(el("span", null, post.audience));
        }

        feedHost.appendChild(
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
                    personButton(post.author_username, post.author_name || displayName, ctx.openPerson),
                    post.post_type === "shared" ? el("span", { className: "meta-chip" }, "공유") : null,
                    post.post_type === "group_post" && !post.group_name ? el("span", { className: "meta-chip" }, "그룹") : null
                  ),
                  el("div", { className: "post-author-sub tabular" }, subParts)
                )
              )
            ),
            !post.caption && post.activity_title
              ? el("div", { className: "activity-subtitle" }, post.activity_title)
              : null,
            post.caption ? renderCollapsibleText(post.caption, { maxChars: 340, maxLines: 6 }) : null,
            sharedNode,
            carousel,
            el(
              "div",
              { className: "post-footer-bar" },
              el("div", { className: "toolbar-group" }, reactionChips),
              el(
                "button",
                {
                  type: "button",
                  className: "btn btn-sm btn-ghost tabular",
                  onPointerenter: () => api.prefetchPost(post.post_id),
                  onClick: () => ctx.openPost(post.post_id),
                },
                icon("comment", 14),
                `댓글 ${post.comments_count || 0}`
              )
            )
          )
        );
      }
    }

    async function loadTimelinePosts(reset = false) {
      if (reset) {
        state.offset = 0;
        state.items = [];
      }
      moreBtn.disabled = true;
      const res = await api.posts({
        platform: "facebook",
        offset: state.offset,
        limit: state.limit,
        year: state.year,
        month: state.month,
        album: state.album,
        post_type: state.postTypeFilter || undefined,
      });
      state.total = res.total;
      state.items.push(...res.items);
      state.offset += res.items.length;
      moreBtn.disabled = false;
      renderPostList();
    }

    moreBtn.addEventListener("click", () => loadTimelinePosts(false));
    await loadTimelinePosts(true);
  }

  function renderAlbums() {
    clear(contentHost);
    if (albumsRes.items.length === 0) {
      contentHost.appendChild(el("div", { className: "empty-box" }, "저장된 Facebook 사진 앨범이 없습니다."));
      return;
    }
    const grid = el("div", { className: "album-grid" });
    for (const alb of albumsRes.items) {
      const coverSrc = mediaUrl(alb.cover_local_path);
      grid.appendChild(
        el(
          "button",
          {
            type: "button",
            className: "card album-card",
            onClick: () => {
              state.subtab = "timeline";
              state.album = alb.album_name;
              state.year = null;
              state.month = null;
              updateSubtabButtons();
              renderTimeline();
            },
          },
          coverSrc
            ? el("img", { className: "album-cover", src: coverSrc, alt: alb.album_name, loading: "lazy", decoding: "async" })
            : el("div", { className: "album-cover" }),
          el(
            "div",
            { className: "album-info" },
            el("div", { style: "font-weight:600;font-size:0.93rem;" }, alb.album_name),
            el("div", { className: "tabular", style: "font-size:0.78rem;color:var(--text-muted);margin-top:2px;" }, `${alb.count}개 항목`)
          )
        )
      );
    }
    contentHost.appendChild(grid);
  }

  async function renderFriends() {
    clear(contentHost);
    let relation = "friend";
    const relSelect = el(
      "select",
      { className: "select", "aria-label": "관계 필터" },
      el("option", { value: "friend", selected: true }, "친구 (Friends)"),
      el("option", { value: "following" }, "팔로잉 (Following)"),
      el("option", { value: "" }, "전체 (친구 + 팔로잉)")
    );
    const searchInput = el("input", {
      type: "search",
      className: "input",
      placeholder: "Facebook 친구·팔로잉 이름 검색...",
    });
    const countLabel = el("span", { className: "toolbar-count tabular" }, "");
    const grid = el("div", { className: "connections-grid" });
    contentHost.append(el("div", { className: "search-bar", style: "align-items:center;" }, relSelect, searchInput, countLabel), grid);

    async function load(q = "") {
      clear(grid);
      const res = await api.connections({
        platform: "facebook",
        relation: relation || undefined,
        q: q || undefined,
        limit: 500,
      });
      countLabel.textContent = `${res.items.length.toLocaleString()} / ${res.total.toLocaleString()}명`;
      clear(grid);
      if (res.items.length === 0) {
        grid.appendChild(el("div", { className: "empty-box" }, "표시할 친구·팔로잉 목록이 없습니다."));
        return;
      }
      for (const f of res.items) {
        grid.appendChild(
          el(
            "div",
            { className: "person-row" },
            avatarEl(f.avatar_local_path, f.full_name || f.username || "?", "md"),
            el(
              "div",
              { style: "min-width:0;flex:1;" },
              personButton(f.username, f.full_name, ctx.openPerson),
              el(
                "div",
                { className: "tabular", style: "font-size:0.76rem;color:var(--text-muted);margin-top:2px;" },
                `${f.relation === "friend" ? "친구" : f.relation}${f.connected_at ? ` · ${formatShortDate(f.connected_at, f.timestamp_unix)}` : ""}`
              )
            )
          )
        );
      }
    }

    let timer = null;
    relSelect.addEventListener("change", () => {
      relation = relSelect.value;
      load(searchInput.value);
    });
    searchInput.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => load(searchInput.value), 180);
    });
    await load();
  }

  async function renderActivities() {
    clear(contentHost);
    let kind = "";
    let q = "";
    let actOffset = 0;
    const actLimit = 100;
    let actTotal = 0;

    const kindSelect = el(
      "select",
      { className: "select", "aria-label": "활동 종류" },
      el("option", { value: "" }, "전체 활동"),
      el("option", { value: "own_comment" }, "내가 쓴 댓글"),
      el("option", { value: "group_comment" }, "그룹에 쓴 댓글"),
      el("option", { value: "saved_item" }, "저장한 항목")
    );
    const qInput = el("input", {
      type: "search",
      className: "input",
      placeholder: "댓글·저장 제목·대상·그룹 검색...",
    });
    const countLabel = el("span", { className: "toolbar-count tabular" }, "");
    const listEl = el("div", { className: "feed-list" });
    const moreWrap = el("div", { className: "load-more-wrap" });
    const moreBtn = el("button", { type: "button", className: "btn" }, "더 보기");
    moreWrap.appendChild(moreBtn);

    contentHost.append(
      el("div", { className: "search-bar", style: "align-items:center;" }, kindSelect, qInput, countLabel),
      listEl,
      moreWrap
    );

    async function load(reset = true) {
      if (reset) {
        actOffset = 0;
        clear(listEl);
      }
      moreBtn.disabled = true;
      const res = await api.activities({
        platform: "facebook",
        kind: kind || undefined,
        q: q || undefined,
        offset: actOffset,
        limit: actLimit,
      });
      actTotal = res.total;
      actOffset += res.items.length;
      countLabel.textContent = `${actOffset.toLocaleString()} / ${actTotal.toLocaleString()}`;
      moreWrap.classList.toggle("hidden", actOffset >= actTotal);
      moreBtn.disabled = false;

      if (reset && res.items.length === 0) {
        listEl.appendChild(el("div", { className: "empty-box" }, "해당하는 활동 내역이 없습니다."));
        return;
      }
      for (const a of res.items) {
        const href = safeExternalHref(a.url);
        const mediaNodes = (a.media || [])
          .map((m) => {
            const src = mediaUrl(m.local_path);
            return src ? el("img", { src, alt: "sticker", loading: "lazy", decoding: "async", style: "max-height:72px;border-radius:6px;" }) : null;
          })
          .filter(Boolean);

        listEl.appendChild(
          el(
            "article",
            { className: "card" },
            el(
              "div",
              { className: "post-meta-row tabular", style: "margin-top:0;margin-bottom:6px;" },
              el("span", { className: "meta-chip" }, a.kind),
              el("span", null, formatDate(a.created_at, a.timestamp_unix)),
              a.group_name ? el("span", { className: "meta-chip" }, a.group_name) : null,
              a.target_author ? el("span", { className: "meta-chip" }, a.target_author) : null
            ),
            a.title ? el("div", { className: "activity-subtitle" }, a.title) : null,
            a.text ? el("div", { className: "post-caption" }, a.text) : null,
            mediaNodes.length > 0 ? el("div", { style: "display:flex;gap:8px;margin-top:6px;" }, mediaNodes) : null,
            href ? el("a", { href, target: "_blank", rel: "noopener noreferrer", style: "font-size:0.8rem;word-break:break-all;" }, a.url) : null
          )
        );
      }
    }

    let timer = null;
    kindSelect.addEventListener("change", () => {
      kind = kindSelect.value;
      load(true);
    });
    qInput.addEventListener("input", () => {
      q = qInput.value;
      clearTimeout(timer);
      timer = setTimeout(() => load(true), 180);
    });
    moreBtn.addEventListener("click", () => load(false));
    await load(true);
  }

  tabTimelineBtn.addEventListener("click", () => {
    state.subtab = "timeline";
    updateSubtabButtons();
    renderTimeline();
  });
  tabAlbumsBtn.addEventListener("click", () => {
    state.subtab = "albums";
    updateSubtabButtons();
    renderAlbums();
  });
  tabFriendsBtn.addEventListener("click", () => {
    state.subtab = "friends";
    updateSubtabButtons();
    renderFriends();
  });
  tabActivitiesBtn.addEventListener("click", () => {
    state.subtab = "activities";
    updateSubtabButtons();
    renderActivities();
  });

  updateSubtabButtons();
  await renderTimeline();
}
