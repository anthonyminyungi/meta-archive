/** Facebook archive view: timeline year/month jump, reactions & nested comments, albums, friends. */

import { api, mediaUrl } from "../api.js";
import {
  avatarEl,
  clear,
  el,
  extractReactionEntries,
  formatDate,
  personButton,
  renderSharedCard,
} from "../dom.js";
import { renderCarousel } from "../components/carousel.js";

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

  const state = {
    subtab: "timeline", // timeline | albums | friends
    year: null,
    month: null,
    album: null,
    postTypeFilter: "", // "" | "original" | "shared"
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
  const tabFriendsBtn = el("button", { type: "button", className: "btn" }, `친구 (${fbStats.connections || 0})`);

  root.appendChild(
    el(
      "section",
      { className: "card profile-header" },
      avatarEl(profile ? profile.avatar_local_path : null, displayName, true),
      el(
        "div",
        { className: "profile-meta" },
        el(
          "div",
          { className: "profile-title-row" },
          el("h1", null, displayName),
          el("span", { className: "pill pill-accent" }, `게시물 ${fbStats.posts || 0}`),
          el("span", { className: "pill" }, `댓글 ${fbStats.comments || 0}`)
        ),
        profile && profile.bio ? el("p", { className: "profile-bio" }, profile.bio) : null,
        el("div", { className: "toolbar-group", style: "margin-top:12px;" }, tabTimelineBtn, tabAlbumsBtn, tabFriendsBtn)
      )
    )
  );

  const contentHost = el("div");
  root.appendChild(contentHost);

  function updateSubtabButtons() {
    tabTimelineBtn.className = `btn${state.subtab === "timeline" ? " btn-primary" : ""}`;
    tabAlbumsBtn.className = `btn${state.subtab === "albums" ? " btn-primary" : ""}`;
    tabFriendsBtn.className = `btn${state.subtab === "friends" ? " btn-primary" : ""}`;
  }

  async function renderTimeline() {
    clear(contentHost);
    const sidebar = el("aside", { className: "card fb-sidebar" });
    const mainCol = el("div");
    const layout = el("div", { className: "fb-layout" }, sidebar, mainCol);
    contentHost.appendChild(layout);

    sidebar.appendChild(el("h3", { style: "margin:0 0 10px;font-size:0.98rem;" }, "연 / 월 이동"));
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
            className: `timeline-btn${!state.year && !state.month ? " active" : ""}`,
            onClick: () => {
              state.year = null;
              state.month = null;
              loadTimelinePosts(true);
            },
          },
          el("span", null, "전체 기간"),
          el("span", null, String(fbStats.posts || 0))
        )
      );
      for (const ym of timelineRes.items) {
        const isActive = state.year === ym.year && state.month === ym.month;
        tmList.appendChild(
          el(
            "button",
            {
              type: "button",
              className: `timeline-btn${isActive ? " active" : ""}`,
              onClick: () => {
                state.year = ym.year;
                state.month = ym.month;
                loadTimelinePosts(true);
              },
            },
            el("span", null, `${ym.year}년 ${String(ym.month).padStart(2, "0")}월`),
            el("span", null, String(ym.count))
          )
        );
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
        ["original", "내 글·사진만"],
        ["shared", "공유 글만"],
      ].map(([val, label]) =>
        el(
          "button",
          {
            type: "button",
            className: `btn${state.postTypeFilter === val ? " btn-primary" : ""}`,
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
          el("span", { className: "pill pill-accent" }, chips.length ? chips.join(" · ") : "전체 기간"),
          el("span", { className: "pill" }, `${state.items.length} / ${state.total}건`),
          chips.length
            ? el(
                "button",
                {
                  type: "button",
                  className: "btn",
                  onClick: () => {
                    state.year = null;
                    state.month = null;
                    state.album = null;
                    loadTimelinePosts(true);
                  },
                },
                "기간/앨범 초기화"
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
        if (post.reactions_total > 0) {
          reactionPills.push(el("span", { className: "pill pill-accent" }, `반응 ${post.reactions_total}`));
        }
        for (const [k, v] of extractReactionEntries(post.reactions)) {
          reactionPills.push(el("span", { className: "pill" }, `${k} ${v}`));
        }

        const sharedNode = renderSharedCard(post.shared_post, ctx.openLightbox);

        feedHost.appendChild(
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
                post.post_type === "shared" ? el("span", { className: "pill pill-warn" }, "공유") : null,
                post.album_name ? el("span", { className: "pill" }, `앨범: ${post.album_name}`) : null,
                post.audience ? el("span", { className: "pill" }, post.audience) : null
              ),
              el("span", { style: "font-size:0.82rem;color:var(--text-muted);" }, formatDate(post.created_at, post.timestamp_unix))
            ),
            post.caption ? el("div", { className: "post-caption" }, post.caption) : null,
            sharedNode,
            carousel,
            el(
              "div",
              { className: "post-meta-row" },
              reactionPills,
              el(
                "button",
                { type: "button", className: "btn", onClick: () => ctx.openPost(post.post_id) },
                `상세 & 댓글 ${post.comments_count} 보기`
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
            el("div", { style: "font-weight:600;" }, alb.album_name),
            el("div", { style: "font-size:0.82rem;color:var(--text-muted);" }, `${alb.count}개 항목`)
          )
        )
      );
    }
    contentHost.appendChild(grid);
  }

  async function renderFriends() {
    clear(contentHost);
    const searchInput = el("input", {
      type: "search",
      className: "input",
      placeholder: "Facebook 친구 이름 또는 아이디 검색...",
    });
    const grid = el("div", { className: "connections-grid" });
    contentHost.append(el("div", { className: "search-bar" }, searchInput), grid);

    async function load(q = "") {
      clear(grid);
      const res = await api.connections({
        platform: "facebook",
        q: q || undefined,
        limit: 300,
      });
      clear(grid);
      if (res.items.length === 0) {
        grid.appendChild(el("div", { className: "empty-box" }, "표시할 친구 목록이 없습니다."));
        return;
      }
      for (const f of res.items) {
        grid.appendChild(
          el(
            "div",
            { className: "person-row" },
            avatarEl(f.avatar_local_path, f.full_name || f.username || "?"),
            el(
              "div",
              { style: "min-width:0;flex:1;" },
              personButton(f.username, f.full_name, ctx.openPerson),
              el(
                "div",
                { style: "font-size:0.78rem;color:var(--text-muted);" },
                `${f.relation}${f.connected_at ? ` · ${formatDate(f.connected_at, f.timestamp_unix)}` : ""}`
              )
            )
          )
        );
      }
    }

    let timer = null;
    searchInput.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => load(searchInput.value), 180);
    });
    await load();
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

  updateSubtabButtons();
  await renderTimeline();
}
