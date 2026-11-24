/** Facebook archive view: year-grouped timeline sidebar, authentic feed cards, rich OG cards, albums, friends. */

import { api, mediaUrl } from "../api.js?v=2";
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
  renderCollapsibleText,
  renderSharedCard,
  safeExternalHref,
} from "../dom.js?v=2";
import { renderCarousel } from "../components/carousel.js?v=2";

export async function renderFacebookView(container, ctx) {
  clear(container);
  container.appendChild(el("div", { className: "loading-box" }, "Facebook 아카이브 불러오는 중..."));

  const [profilesRes, timelineRes, albumsRes, overview] = await Promise.all([
    api.profiles("facebook"),
    api.timeline("facebook"),
    api.albums("facebook"),
    api.overview(),
  ]);

  const profile = profilesRes.items[0] || null;
  const fbStats = (overview.by_platform && overview.by_platform.facebook) || {};

  // Group timeline months by year for compact accordion sidebar
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
    postTypeFilter: "", // "" | "original" | "group_post" | "shared"
    offset: 0,
    limit: 25,
    total: 0,
    items: [],
  };

  clear(container);
  const root = el("div");
  container.appendChild(root);

  const displayName = profile ? profile.full_name || profile.username || "Facebook" : "Facebook Archive";
  const tabTimelineBtn = el("button", { type: "button", className: "btn btn-primary" }, "타임라인");
  const tabAlbumsBtn = el("button", { type: "button", className: "btn" }, `사진 앨범 (${albumsRes.items.length})`);
  const tabFriendsBtn = el("button", { type: "button", className: "btn" }, `친구·팔로잉 (${(fbStats.connections || 0).toLocaleString()})`);
  const tabActivitiesBtn = el("button", { type: "button", className: "btn" }, `내 댓글·저장됨 (${(fbStats.activities || 0).toLocaleString()})`);

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
              { className: "post-meta-row", style: "margin-top:0;font-size:0.75rem;" },
              el("span", { className: "pill pill-fb" }, formatDate(s.created_at, s.timestamp_unix))
            ),
            src
              ? isVideoMedia(s.media)
                ? el("video", { src, controls: true, playsinline: true, preload: "metadata" })
                : el("img", {
                    src,
                    alt: "story",
                    loading: "lazy",
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
      { className: "card profile-header" },
      avatarEl(profile ? profile.avatar_local_path : null, displayName, "lg"),
      el(
        "div",
        { className: "profile-meta" },
        el(
          "div",
          { className: "profile-title-row" },
          el("h1", null, displayName),
          el("span", { className: "pill pill-fb" }, `게시물 ${(fbStats.posts || 0).toLocaleString()}`),
          el("span", { className: "pill" }, `댓글 ${(fbStats.comments || 0).toLocaleString()}`),
          (fbStats.stories || 0) > 0
            ? el("button", { type: "button", className: "btn btn-sm", onClick: openFbStoriesModal }, `보관 스토리 (${fbStats.stories})`)
            : null
        ),
        profile && profile.bio ? el("p", { className: "profile-bio" }, profile.bio) : null,
        el("div", { className: "toolbar-group", style: "margin-top:12px;" }, tabTimelineBtn, tabAlbumsBtn, tabFriendsBtn, tabActivitiesBtn)
      )
    )
  );

  const contentHost = el("div");
  root.appendChild(contentHost);

  function updateSubtabButtons() {
    tabTimelineBtn.className = `btn${state.subtab === "timeline" ? " btn-primary" : ""}`;
    tabAlbumsBtn.className = `btn${state.subtab === "albums" ? " btn-primary" : ""}`;
    tabFriendsBtn.className = `btn${state.subtab === "friends" ? " btn-primary" : ""}`;
    tabActivitiesBtn.className = `btn${state.subtab === "activities" ? " btn-primary" : ""}`;
  }

  async function renderTimeline() {
    clear(contentHost);
    const sidebar = el("aside", { className: "card fb-sidebar" });
    const mainCol = el("div");
    const layout = el("div", { className: "fb-layout" }, sidebar, mainCol);
    contentHost.appendChild(layout);

    sidebar.appendChild(el("h3", { style: "margin:0 0 10px;font-size:0.95rem;" }, "연도 / 월 이동"));
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
          el("span", { style: "font-weight:500;font-size:0.8rem;" }, (fbStats.posts || 0).toLocaleString())
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
          el("span", null, `${isExpanded ? "▾" : "▸"} ${grp.year}년`),
          el("span", { style: "font-weight:500;font-size:0.8rem;color:var(--text-muted);" }, String(grp.total))
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
                    className: `timeline-btn${isActive ? " active" : ""}`,
                    onClick: () => {
                      state.year = ym.year;
                      state.month = ym.month;
                      state.expandedYear = ym.year;
                      loadTimelinePosts(true);
                    },
                  },
                  el("span", null, `${String(ym.month).padStart(2, "0")}월`),
                  el("span", null, String(ym.count))
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
      if (state.album) chips.push(`앨범: ${state.album}`);

      const typeButtons = [
        ["", "전체"],
        ["original", "내 글·사진"],
        ["group_post", "그룹 글"],
        ["shared", "공유 글"],
      ].map(([val, label]) =>
        el(
          "button",
          {
            type: "button",
            className: `btn btn-sm${state.postTypeFilter === val ? " btn-primary" : ""}`,
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
          el("span", { className: "pill pill-fb" }, chips.length ? chips.join(" · ") : "전체 기간"),
          el("span", { className: "pill" }, `${state.items.length.toLocaleString()} / ${state.total.toLocaleString()}건`),
          chips.length
            ? el(
                "button",
                {
                  type: "button",
                  className: "btn btn-sm",
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
      activeFilterRow.appendChild(el("div", { className: "toolbar-group" }, typeButtons));

      clear(feedHost);
      moreWrap.classList.toggle("hidden", state.items.length >= state.total);
      if (state.items.length === 0) {
        feedHost.appendChild(el("div", { className: "empty-box" }, "해당 조건의 Facebook 게시물이 없습니다."));
        return;
      }

      for (const post of state.items) {
        const carousel = renderCarousel(post.media, ctx.openLightbox);
        const reactionPills = [];
        const entries = extractReactionEntries(post.reactions);
        if (entries.length > 0) {
          for (const [k, v] of entries) {
            reactionPills.push(el("span", { className: "pill" }, formatReactionBadge(k, v)));
          }
        } else if (post.reactions_total > 0) {
          reactionPills.push(el("span", { className: "pill pill-fb" }, `👍 반응 ${post.reactions_total}`));
        }

        const sharedNode = renderSharedCard(post.shared_post, ctx.openLightbox);
        const authorLabel = post.author_name || post.author_username || displayName;

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
                    { style: "display:flex;gap:6px;align-items:center;flex-wrap:wrap;" },
                    personButton(post.author_username, post.author_name || displayName, ctx.openPerson),
                    post.post_type === "shared" ? el("span", { className: "pill pill-warn" }, "공유") : null,
                    post.post_type === "group_post"
                      ? el("span", { className: "pill pill-fb" }, post.group_name ? `그룹: ${post.group_name}` : "그룹 글")
                      : null,
                    post.album_name ? el("span", { className: "pill" }, `📁 ${post.album_name}`) : null
                  ),
                  el(
                    "div",
                    { className: "post-author-sub" },
                    el("span", null, formatDate(post.created_at, post.timestamp_unix)),
                    post.audience ? el("span", null, `· ${post.audience}`) : null
                  )
                )
              )
            ),
            !post.caption && post.activity_title
              ? el("div", { style: "font-size:0.86rem;color:var(--text-muted);margin-bottom:6px;" }, post.activity_title)
              : null,
            post.caption ? renderCollapsibleText(post.caption, { maxChars: 340, maxLines: 6 }) : null,
            sharedNode,
            carousel,
            el(
              "div",
              { className: "post-footer-bar" },
              el("div", { className: "toolbar-group" }, reactionPills),
              el(
                "button",
                { type: "button", className: "btn btn-sm", onClick: () => ctx.openPost(post.post_id) },
                `💬 상세 & 댓글 ${post.comments_count}`
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
            ? el("img", { className: "album-cover", src: coverSrc, alt: alb.album_name, loading: "lazy" })
            : el("div", { className: "album-cover" }),
          el(
            "div",
            { className: "album-info" },
            el("div", { style: "font-weight:700;" }, alb.album_name),
            el("div", { style: "font-size:0.8rem;color:var(--text-muted);margin-top:2px;" }, `${alb.count}개 항목`)
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
    const countPill = el("span", { className: "pill" }, "");
    const grid = el("div", { className: "connections-grid" });
    contentHost.append(el("div", { className: "search-bar" }, relSelect, searchInput, countPill), grid);

    async function load(q = "") {
      clear(grid);
      const res = await api.connections({
        platform: "facebook",
        relation: relation || undefined,
        q: q || undefined,
        limit: 500,
      });
      countPill.textContent = `${res.items.length.toLocaleString()} / ${res.total.toLocaleString()}명`;
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
                { style: "font-size:0.76rem;color:var(--text-muted);margin-top:2px;" },
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
      el("option", { value: "" }, "전체 활동 (댓글·그룹댓글·저장됨)"),
      el("option", { value: "own_comment" }, "내가 쓴 댓글"),
      el("option", { value: "group_comment" }, "그룹에 쓴 댓글"),
      el("option", { value: "saved_item" }, "저장한 항목")
    );
    const qInput = el("input", {
      type: "search",
      className: "input",
      placeholder: "댓글·저장 제목·대상·그룹 검색...",
    });
    const countPill = el("span", { className: "pill" }, "0건");
    const listEl = el("div", { className: "feed-list" });
    const moreWrap = el("div", { className: "load-more-wrap" });
    const moreBtn = el("button", { type: "button", className: "btn" }, "더 보기");
    moreWrap.appendChild(moreBtn);

    contentHost.append(
      el("div", { className: "search-bar" }, kindSelect, qInput, countPill),
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
      countPill.textContent = `${actOffset.toLocaleString()} / ${actTotal.toLocaleString()}건`;
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
            return src ? el("img", { src, alt: "sticker", loading: "lazy", style: "max-height:72px;border-radius:6px;" }) : null;
          })
          .filter(Boolean);

        listEl.appendChild(
          el(
            "article",
            { className: "card" },
            el(
              "div",
              { className: "post-meta-row", style: "margin-top:0;margin-bottom:6px;" },
              el("span", { className: "pill pill-fb" }, a.kind),
              el("span", { className: "pill" }, formatDate(a.created_at, a.timestamp_unix)),
              a.group_name ? el("span", { className: "pill" }, `그룹: ${a.group_name}`) : null,
              a.target_author ? el("span", { className: "pill" }, `대상: ${a.target_author}`) : null
            ),
            a.title ? el("div", { style: "font-size:0.84rem;color:var(--text-muted);margin-bottom:6px;" }, a.title) : null,
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
