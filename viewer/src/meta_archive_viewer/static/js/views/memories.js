/** Memories view: On This Day time machine, 2012-2026 Yearbook, Inner Circle (IG+FB), and Unified Photo Wall + Keepsakes. */

import { api, mediaUrl } from "../api.js?v=5";
import {
  avatarEl,
  clear,
  el,
  formatDate,
  formatShortDate,
  isVideoMedia,
  skeletonCards,
  skeletonGrid,
} from "../dom.js?v=5";
import { icon } from "../icons.js?v=5";

const KEEPSAKES_KEY = "meta-archive-keepsakes-v1";
const DAYS_IN_MONTH = [0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

function loadKeepsakes() {
  try {
    const raw = localStorage.getItem(KEEPSAKES_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) return new Set();
    return new Set(arr.map((x) => Number(x)).filter((n) => Number.isFinite(n) && n > 0));
  } catch {
    return new Set();
  }
}

function saveKeepsakes(set) {
  try {
    localStorage.setItem(KEEPSAKES_KEY, JSON.stringify(Array.from(set)));
  } catch {
    // ignore storage quota errors
  }
}

function formatYearSpan(firstTs, lastTs) {
  const y1 = firstTs ? new Date(Number(firstTs) * 1000).getFullYear() : null;
  const y2 = lastTs ? new Date(Number(lastTs) * 1000).getFullYear() : null;
  if (y1 && y2) return y1 === y2 ? `${y1}` : `${y1} — ${y2}`;
  return y1 ? `${y1}` : y2 ? `${y2}` : "";
}

function shiftCalendarDay(month, day, delta) {
  let m = month;
  let d = day + delta;
  if (d < 1) {
    m = m === 1 ? 12 : m - 1;
    d = DAYS_IN_MONTH[m];
  } else if (d > DAYS_IN_MONTH[m]) {
    d = 1;
    m = m === 12 ? 1 : m + 1;
  }
  return { month: m, day: d };
}

function platformTag(platform) {
  const isIg = platform === "instagram";
  return el(
    "span",
    { className: `plat-pill ${isIg ? "ig" : "fb"}` },
    isIg ? "IG" : "FB"
  );
}

export async function renderMemoriesView(container, ctx, params = new URLSearchParams()) {
  clear(container);
  container.appendChild(skeletonCards(3));

  const now = new Date();
  const keepsakes = loadKeepsakes();

  const state = {
    subtab: params.get("tab") || "onthisday", // onthisday | yearbook | people | photos
    // On This Day
    month: Number(params.get("month")) || now.getMonth() + 1,
    day: Number(params.get("day")) || now.getDate(),
    includeShared: false,
    // Yearbook
    year: params.get("year") ? Number(params.get("year")) : null,
    // Photo Wall
    photoYear: null,
    photoPlatform: "",
    photoPlace: params.get("place") || "",
    photoSort: "recent", // recent | oldest | random
    photoFavoritesOnly: false,
    photoOffset: 0,
    photoLimit: 60,
    photoTotal: 0,
    photoItems: [],
    photoPlaces: [],
    photoYears: [],
  };

  const [profilesRes, overview] = await Promise.all([
    api.profiles(),
    api.overview(),
  ]);

  const igProf = (profilesRes.items || []).find((p) => p.platform === "instagram");
  const fbProf = (profilesRes.items || []).find((p) => p.platform === "facebook");
  const mainProf = igProf || fbProf || null;
  const totals = overview.totals || {};

  clear(container);
  const root = el("div", { className: "mem-root" });
  container.appendChild(root);

  // Editorial Hero Header
  const hero = el(
    "section",
    { className: "mem-hero card" },
    el(
      "div",
      { className: "mem-hero-left" },
      avatarEl(mainProf ? mainProf.avatar_local_path : null, mainProf ? mainProf.full_name || "Me" : "Me", "lg"),
      el(
        "div",
        { className: "mem-hero-copy" },
        el(
          "div",
          { className: "mem-eyebrow" },
          icon("sparkles", 13),
          "PERSONAL ARCHIVE · 2012 — 2026"
        ),
        el(
          "h1",
          { className: "mem-title" },
          `${mainProf ? mainProf.full_name || mainProf.username : "나의"} 14년의 기록`
        ),
        el(
          "div",
          { className: "mem-hero-stats tabular" },
          el("span", null, `게시물 ${(totals.posts || 0).toLocaleString()}`),
          el("span", { className: "mem-dot" }, "·"),
          el("span", null, `스토리 ${(totals.stories || 0).toLocaleString()}`),
          el("span", { className: "mem-dot" }, "·"),
          el("span", null, `메시지 ${(totals.messages || 0).toLocaleString()}`),
          el("span", { className: "mem-dot" }, "·"),
          el("span", null, `댓글 ${(totals.comments || 0).toLocaleString()}`)
        )
      )
    )
  );

  // Segmented Sub-navigation
  const tabDefs = [
    { key: "onthisday", label: "오늘의 추억", iconName: "calendar" },
    { key: "yearbook", label: "연감 (2012–2026)", iconName: "book" },
    { key: "people", label: "함께한 사람들", iconName: "users" },
    { key: "photos", label: "포토월", iconName: "image" },
  ];
  const tabBtns = new Map();
  const subnav = el(
    "div",
    { className: "segmented-control mem-subnav", role: "tablist" },
    tabDefs.map((td) => {
      const btn = el(
        "button",
        {
          type: "button",
          role: "tab",
          className: `seg-btn${state.subtab === td.key ? " active" : ""}`,
          onClick: () => switchSubtab(td.key),
        },
        icon(td.iconName, 14),
        td.label
      );
      tabBtns.set(td.key, btn);
      return btn;
    })
  );

  const stageEl = el("div", { className: "mem-stage" });
  root.append(hero, subnav, stageEl);

  let subtabSeq = 0;
  let photoGridEl = null;
  let photoLbMedia = [];
  let photoFavBtn = null;
  let photoMoreBtn = null;

  function renderStageError(err) {
    clear(stageEl);
    stageEl.appendChild(
      el("div", { className: "error-box" }, `추억 데이터를 불러오지 못했습니다: ${err.message}`)
    );
  }

  function switchSubtab(nextTab) {
    state.subtab = nextTab;
    for (const [k, btn] of tabBtns.entries()) {
      btn.classList.toggle("active", k === nextTab);
    }
    renderActiveSubtab();
  }

  function makePostMemoryCard(post, yearBadgeText = null) {
    const firstMedia = (post.media || [])[0] || null;
    const mediaSrc = firstMedia ? mediaUrl(firstMedia.local_path) : null;
    const isVid = firstMedia ? isVideoMedia(firstMedia) : false;
    const mediaCount = (post.media || []).length;

    const thumbEl = mediaSrc
      ? el(
          "div",
          { className: "mem-post-thumb" },
          isVid
            ? el("video", { src: mediaSrc, muted: true, playsinline: true, preload: "metadata" })
            : el("img", { src: mediaSrc, alt: "memory", loading: "lazy", decoding: "async" }),
          isVid ? el("span", { className: "mem-media-badge" }, icon("play", 11), "영상") : null,
          mediaCount > 1 ? el("span", { className: "mem-media-badge right tabular" }, icon("layers", 11), mediaCount) : null
        )
      : null;

    const bodyText = (post.caption || "").trim() || (post.shared_post && (post.shared_post.title || post.shared_post.text)) || post.activity_title || "(사진/기록)";

    return el(
      "button",
      {
        type: "button",
        className: `card mem-post-card${thumbEl ? " has-thumb" : ""}`,
        onClick: () => ctx.openPost(post.post_id),
        onMouseEnter: () => api.prefetchPost(post.post_id),
      },
      thumbEl,
      el(
        "div",
        { className: "mem-post-body" },
        el(
          "div",
          { className: "mem-post-meta tabular" },
          platformTag(post.platform),
          yearBadgeText ? el("span", { className: "meta-chip" }, yearBadgeText) : null,
          el("span", { className: "mem-date-muted" }, formatDate(post.created_at, post.timestamp_unix)),
          post.is_archived ? el("span", { className: "meta-chip" }, icon("archive", 11), "보관") : null
        ),
        el("div", { className: "mem-post-text" }, bodyText),
        el(
          "div",
          { className: "mem-post-foot tabular" },
          post.location_name
            ? el("span", { className: "inline-meta" }, icon("pin", 12), post.location_name)
            : null,
          post.likes_count > 0 || post.reactions_total > 0
            ? el("span", { className: "inline-meta" }, icon("heart", 12), (post.likes_count || post.reactions_total).toLocaleString())
            : null,
          post.comment_count > 0
            ? el("span", { className: "inline-meta" }, icon("comment", 12), post.comment_count.toLocaleString())
            : null
        )
      )
    );
  }

  function makePhotoTile(item, allLightboxItems, idx, onKeepsakeChange = null) {
    const src = mediaUrl(item.local_path);
    const isVid = isVideoMedia(item);
    const isStarred = keepsakes.has(Number(item.id));

    const starBtn = el(
      "button",
      {
        type: "button",
        className: `mem-star-btn${isStarred ? " active" : ""}`,
        title: isStarred ? "즐겨찾기 해제" : "즐겨찾기에 담기",
        "aria-label": isStarred ? "즐겨찾기 해제" : "즐겨찾기에 담기",
        onClick: (ev) => {
          ev.stopPropagation();
          const numId = Number(item.id);
          if (keepsakes.has(numId)) keepsakes.delete(numId);
          else keepsakes.add(numId);
          saveKeepsakes(keepsakes);
          const nowStarred = keepsakes.has(numId);
          starBtn.classList.toggle("active", nowStarred);
          clear(starBtn);
          starBtn.appendChild(icon(nowStarred ? "starFilled" : "star", 14));
          if (onKeepsakeChange) onKeepsakeChange();
        },
      },
      icon(isStarred ? "starFilled" : "star", 14)
    );

    const openPostBtn =
      item.owner_type === "post" && item.owner_id
        ? el(
            "button",
            {
              type: "button",
              className: "mem-tile-post-btn",
              title: "원본 게시물 보기",
              onClick: (ev) => {
                ev.stopPropagation();
                ctx.openPost(item.owner_id);
              },
            },
            icon("external", 12)
          )
        : null;

    return el(
      "div",
      {
        className: "mem-photo-tile",
        role: "button",
        tabindex: "0",
        onClick: () => ctx.openLightbox(allLightboxItems, idx),
        onKeydown: (ev) => {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            ctx.openLightbox(allLightboxItems, idx);
          }
        },
      },
      src
        ? isVid
          ? el("video", { src, muted: true, playsinline: true, preload: "metadata" })
          : el("img", { src, alt: item.caption || "photo", loading: "lazy", decoding: "async" })
        : null,
      isVid ? el("span", { className: "mem-tile-vid-badge" }, icon("play", 11)) : null,
      starBtn,
      openPostBtn,
      el(
        "div",
        { className: "mem-photo-overlay tabular" },
        el(
          "span",
          { className: "mem-photo-date" },
          platformTag(item.platform),
          item.owner_type === "story" ? " 스토리 · " : " ",
          formatShortDate(item.created_at, item.timestamp_unix)
        ),
        item.location_name ? el("span", { className: "mem-photo-place" }, item.location_name) : null
      )
    );
  }

  // ------------------------------------------------------------------------
  // Subtab 1: On This Day + Time Machine
  // ------------------------------------------------------------------------
  async function renderOnThisDay(randomDay = false) {
    const seq = ++subtabSeq;
    clear(stageEl);
    stageEl.appendChild(skeletonCards(3));

    try {
      const data = await api.memoriesOnThisDay(
        randomDay
          ? { random_day: true, include_shared: state.includeShared }
          : { month: state.month, day: state.day, include_shared: state.includeShared }
      );
      if (seq !== subtabSeq) return;
      state.month = data.month;
      state.day = data.day;

      clear(stageEl);

    const monthSelect = el(
      "select",
      { className: "select tabular", "aria-label": "월 선택" },
      Array.from({ length: 12 }, (_, idx) => {
        const m = idx + 1;
        return el("option", { value: m, selected: m === state.month }, `${m}월`);
      })
    );
    const daySelect = el(
      "select",
      { className: "select tabular", "aria-label": "일 선택" },
      Array.from({ length: DAYS_IN_MONTH[state.month] }, (_, idx) => {
        const d = idx + 1;
        return el("option", { value: d, selected: d === state.day }, `${d}일`);
      })
    );
    monthSelect.addEventListener("change", () => {
      state.month = Number(monthSelect.value);
      state.day = Math.min(state.day, DAYS_IN_MONTH[state.month]);
      renderOnThisDay(false);
    });
    daySelect.addEventListener("change", () => {
      state.day = Number(daySelect.value);
      renderOnThisDay(false);
    });

    const sharedCheckbox = el("input", {
      type: "checkbox",
      checked: state.includeShared,
      onChange: () => {
        state.includeShared = sharedCheckbox.checked;
        renderOnThisDay(false);
      },
    });

    const isToday = state.month === now.getMonth() + 1 && state.day === now.getDate();

    const controls = el(
      "div",
      { className: "card mem-otd-bar" },
      el(
        "div",
        { className: "mem-otd-date-head" },
        el(
          "button",
          {
            type: "button",
            className: "icon-btn",
            title: "이전 날짜",
            onClick: () => {
              const prev = shiftCalendarDay(state.month, state.day, -1);
              state.month = prev.month;
              state.day = prev.day;
              renderOnThisDay(false);
            },
          },
          icon("chevronLeft", 16)
        ),
        el(
          "div",
          { className: "mem-otd-title-wrap" },
          el("h2", { className: "mem-section-title tabular" }, `${state.month}월 ${state.day}일의 추억`),
          el(
            "span",
            { className: "mem-otd-sub tabular" },
            data.years_active.length > 0
              ? `${data.years_active.length}개 연도 · 게시물 ${data.posts.length} · 스토리 ${data.stories.length} · 대화 ${data.message_highlights.length}`
              : "이 날짜에는 기록이 없습니다"
          )
        ),
        el(
          "button",
          {
            type: "button",
            className: "icon-btn",
            title: "다음 날짜",
            onClick: () => {
              const next = shiftCalendarDay(state.month, state.day, 1);
              state.month = next.month;
              state.day = next.day;
              renderOnThisDay(false);
            },
          },
          icon("chevronRight", 16)
        )
      ),
      el(
        "div",
        { className: "mem-otd-actions" },
        monthSelect,
        daySelect,
        !isToday
          ? el(
              "button",
              {
                type: "button",
                className: "btn btn-sm",
                onClick: () => {
                  state.month = now.getMonth() + 1;
                  state.day = now.getDate();
                  renderOnThisDay(false);
                },
              },
              "오늘"
            )
          : null,
        el(
          "button",
          {
            type: "button",
            className: "btn btn-primary btn-sm",
            onClick: () => renderOnThisDay(true),
          },
          icon("shuffle", 14),
          "랜덤 하루 뽑기"
        ),
        el("label", { className: "mem-toggle-label" }, sharedCheckbox, "FB 공유글 포함")
      )
    );
    stageEl.appendChild(controls);

    if (data.years_active.length === 0) {
      stageEl.appendChild(
        el(
          "div",
          { className: "empty-box", style: "margin-top:16px;" },
          `${state.month}월 ${state.day}일에는 저장된 기록이 없습니다. '랜덤 하루 뽑기'로 다른 날의 추억을 열어보세요.`
        )
      );
      return;
    }

    const postsByYear = new Map();
    for (const p of data.posts) {
      const y = p.year || 0;
      if (!postsByYear.has(y)) postsByYear.set(y, []);
      postsByYear.get(y).push(p);
    }
    const storiesByYear = new Map();
    const allStoryMedia = data.stories.map((s) => s.media).filter((m) => m && m.local_path);
    for (const s of data.stories) {
      const y = s.year || 0;
      if (!storiesByYear.has(y)) storiesByYear.set(y, []);
      storiesByYear.get(y).push(s);
    }
    const msgsByYear = new Map();
    for (const m of data.message_highlights) {
      const y = m.year || 0;
      if (!msgsByYear.has(y)) msgsByYear.set(y, []);
      msgsByYear.get(y).push(m);
    }

    const thisYear = now.getFullYear();
    const timelineWrap = el("div", { className: "mem-otd-years" });
    for (const y of data.years_active) {
      const yPosts = postsByYear.get(y) || [];
      const yStories = storiesByYear.get(y) || [];
      const yMsgs = msgsByYear.get(y) || [];
      const diff = thisYear - y;
      const agoLabel = diff > 0 ? `${diff}년 전` : diff === 0 ? "올해" : "";

      const section = el(
        "section",
        { className: "mem-year-block" },
        el(
          "div",
          { className: "mem-year-heading" },
          el("span", { className: "mem-year-num tabular" }, String(y)),
          agoLabel ? el("span", { className: "meta-chip" }, agoLabel) : null
        )
      );

      if (yPosts.length > 0) {
        section.appendChild(
          el(
            "div",
            { className: "mem-post-grid" },
            yPosts.map((p) => makePostMemoryCard(p))
          )
        );
      }

      if (yStories.length > 0) {
        section.appendChild(
          el(
            "div",
            { className: "mem-story-strip" },
            yStories.map((s) => {
              const src = s.media ? mediaUrl(s.media.local_path) : null;
              const lbIdx = s.media ? allStoryMedia.indexOf(s.media) : -1;
              const isVid = isVideoMedia(s.media);
              return el(
                "button",
                {
                  type: "button",
                  className: "mem-story-chip-card",
                  onClick: () => {
                    if (lbIdx >= 0) ctx.openLightbox(allStoryMedia, lbIdx);
                  },
                },
                src
                  ? isVid
                    ? el("video", { src, muted: true, playsinline: true, preload: "metadata" })
                    : el("img", { src, alt: "story", loading: "lazy", decoding: "async" })
                  : el("div", { className: "mem-story-empty" }, "스토리"),
                el(
                  "span",
                  { className: "mem-story-caption" },
                  s.highlight_title || (s.caption ? s.caption.slice(0, 20) : "스토리")
                )
              );
            })
          )
        );
      }

      if (yMsgs.length > 0) {
        section.appendChild(
          el(
            "div",
            { className: "mem-msg-strip" },
            yMsgs.slice(0, 6).map((m) =>
              el(
                "button",
                {
                  type: "button",
                  className: "card mem-msg-pill",
                  onClick: () => ctx.navigateThread(m.thread_id, m.jump_ts),
                },
                avatarEl(m.avatar_local_path, m.title, "sm"),
                el(
                  "div",
                  { className: "mem-msg-pill-body" },
                  el(
                    "div",
                    { className: "mem-msg-pill-top" },
                    platformTag(m.platform),
                    el("strong", null, m.title),
                    el("span", { className: "mem-date-muted tabular" }, `${m.message_count}건`)
                  ),
                  m.snippet ? el("div", { className: "mem-msg-snippet" }, m.snippet) : null
                )
              )
            )
          )
        );
      }

      timelineWrap.appendChild(section);
    }

    stageEl.appendChild(timelineWrap);
    } catch (err) {
      if (seq !== subtabSeq) return;
      renderStageError(err);
    }
  }

  // ------------------------------------------------------------------------
  // Subtab 2: Yearbook (2012 - 2026)
  // ------------------------------------------------------------------------
  async function renderYearbook() {
    const seq = ++subtabSeq;
    clear(stageEl);
    stageEl.appendChild(skeletonCards(3));

    try {
      const data = await api.memoriesYearbook(state.year ? { year: state.year } : {});
      if (seq !== subtabSeq) return;
      state.year = data.selected_year;

      clear(stageEl);

    // 14-year interactive activity strip
    const maxWeight = Math.max(
      1,
      ...data.years.map((y) => y.total_memories * 15 + y.messages)
    );
    const yearStrip = el(
      "div",
      { className: "card mem-year-strip", role: "tablist", "aria-label": "연도별 연감" },
      data.years.map((y) => {
        const weight = y.total_memories * 15 + y.messages;
        const pct = Math.max(10, Math.round((weight / maxWeight) * 100));
        const active = y.year === state.year;
        return el(
          "button",
          {
            type: "button",
            className: `mem-year-pill${active ? " active" : ""}`,
            title: `${y.year}년 · IG ${y.ig_posts} · FB 원글 ${y.fb_orig} · 스토리 ${y.stories} · 메시지 ${y.messages.toLocaleString()}`,
            onClick: () => {
              state.year = y.year;
              renderYearbook();
            },
          },
          el(
            "div",
            { className: "mem-year-bar-track" },
            el("div", { className: "mem-year-bar-fill", style: `height:${pct}%` })
          ),
          el("span", { className: "mem-year-pill-label tabular" }, String(y.year))
        );
      })
    );
    stageEl.appendChild(yearStrip);

    const st = data.stats || {};
    const headerCard = el(
      "section",
      { className: "card mem-yb-head" },
      el(
        "div",
        { className: "mem-yb-head-top" },
        el("h2", { className: "mem-yb-year tabular" }, `${data.selected_year}`),
        el(
          "div",
          { className: "mem-yb-metrics tabular" },
          el("span", { className: "meta-chip" }, `IG 피드 ${st.ig_posts || 0}`),
          el("span", { className: "meta-chip" }, `FB 원글 ${st.fb_orig || 0}`),
          el("span", { className: "meta-chip" }, `스토리 ${st.stories || 0}`),
          el("span", { className: "meta-chip" }, `메시지 ${(st.messages || 0).toLocaleString()}`),
          el("span", { className: "meta-chip" }, `댓글 ${(st.comments || 0).toLocaleString()}`)
        )
      ),
      data.places && data.places.length > 0
        ? el(
            "div",
            { className: "mem-places-row" },
            el("span", { className: "mem-places-label" }, icon("pin", 13), "다녀온 장소"),
            data.places.map((pl) =>
              el(
                "button",
                {
                  type: "button",
                  className: "mem-place-chip",
                  onClick: () => {
                    state.photoPlace = pl.name;
                    state.photoYear = null;
                    state.photoFavoritesOnly = false;
                    switchSubtab("photos");
                  },
                },
                pl.name,
                el("span", { className: "tabular mem-place-cnt" }, pl.count)
              )
            )
          )
        : null
    );
    stageEl.appendChild(headerCard);

    // Year photo filmstrip
    if (data.photos && data.photos.length > 0) {
      const photosTotal = data.photos_total ?? data.photos.length;
      const lbMedia = data.photos.map((p) => ({
        local_path: p.local_path,
        media_type: p.media_type,
      }));
      stageEl.appendChild(
        el(
          "section",
          { className: "mem-section" },
          el(
            "div",
            { className: "mem-section-head" },
            el("h3", { className: "mem-h3" }, `${data.selected_year}년의 장면들 (${photosTotal.toLocaleString()})`),
            el(
              "button",
              {
                type: "button",
                className: "btn btn-ghost btn-sm",
                onClick: () => {
                  state.photoYear = data.selected_year;
                  state.photoPlace = "";
                  state.photoFavoritesOnly = false;
                  switchSubtab("photos");
                },
              },
              "포토월에서 전체 보기"
            )
          ),
          el(
            "div",
            { className: "mem-photo-grid compact" },
            data.photos.slice(0, 18).map((item, idx) => makePhotoTile(item, lbMedia, idx))
          )
        )
      );
    }

    // People of that year (2-col: threads + commenters)
    if ((data.top_threads && data.top_threads.length > 0) || (data.top_commenters && data.top_commenters.length > 0)) {
      stageEl.appendChild(
        el(
          "section",
          { className: "mem-section mem-yb-people-grid" },
          data.top_threads && data.top_threads.length > 0
            ? el(
                "div",
                { className: "card mem-yb-col" },
                el("h3", { className: "mem-h3", style: "margin-bottom:10px;" }, "그 해 가장 많이 대화한 방"),
                el(
                  "div",
                  { className: "mem-mini-list" },
                  data.top_threads.map((t) =>
                    el(
                      "button",
                      {
                        type: "button",
                        className: "mem-mini-row",
                        onClick: () => ctx.navigateThread(t.thread_id, t.jump_ts),
                      },
                      avatarEl(t.avatar_local_path, t.title, "sm"),
                      el("span", { className: "mem-mini-name" }, t.title),
                      platformTag(t.platform),
                      el("span", { className: "mem-date-muted tabular" }, `${t.message_count.toLocaleString()}건`)
                    )
                  )
                )
              )
            : null,
          data.top_commenters && data.top_commenters.length > 0
            ? el(
                "div",
                { className: "card mem-yb-col" },
                el("h3", { className: "mem-h3", style: "margin-bottom:10px;" }, "그 해 자주 댓글을 남긴 친구"),
                el(
                  "div",
                  { className: "mem-mini-list" },
                  data.top_commenters.map((c) =>
                    el(
                      "button",
                      {
                        type: "button",
                        className: "mem-mini-row",
                        onClick: () => ctx.openPerson(c.identifier),
                      },
                      avatarEl(c.avatar_local_path, c.label, "sm"),
                      el("span", { className: "mem-mini-name" }, c.label),
                      platformTag(c.platform),
                      el("span", { className: "mem-date-muted tabular" }, `댓글 ${c.comment_count}`)
                    )
                  )
                )
              )
            : null
        )
      );
    }

    // Original posts of that year (with incremental Load More when > 40)
    if (data.posts && data.posts.length > 0) {
      const postsTotal = data.posts_total ?? data.posts.length;
      let loadedCount = data.posts.length;
      const postGrid = el(
        "div",
        { className: "mem-post-grid" },
        data.posts.map((p) => makePostMemoryCard(p))
      );
      const section = el(
        "section",
        { className: "mem-section" },
        el("h3", { className: "mem-h3", style: "margin-bottom:12px;" }, `${data.selected_year}년의 기록 (${postsTotal.toLocaleString()})`),
        postGrid
      );
      if (loadedCount < postsTotal) {
        const morePostsBtn = el(
          "button",
          {
            type: "button",
            className: "btn",
            style: "display:block;margin:18px auto 0;",
            onClick: async () => {
              morePostsBtn.disabled = true;
              try {
                const next = await api.memoriesYearbook({
                  year: data.selected_year,
                  posts_offset: loadedCount,
                  posts_limit: 40,
                });
                if (seq !== subtabSeq) return;
                for (const p of next.posts || []) {
                  postGrid.appendChild(makePostMemoryCard(p));
                }
                loadedCount += (next.posts || []).length;
                if (loadedCount >= postsTotal || (next.posts || []).length === 0) {
                  morePostsBtn.remove();
                } else {
                  morePostsBtn.disabled = false;
                  morePostsBtn.textContent = `기록 더 보기 (${loadedCount.toLocaleString()} / ${postsTotal.toLocaleString()})`;
                }
              } catch (err) {
                morePostsBtn.disabled = false;
                morePostsBtn.textContent = `다시 시도 (${err.message})`;
              }
            },
          },
          `기록 더 보기 (${loadedCount.toLocaleString()} / ${postsTotal.toLocaleString()})`
        );
        section.appendChild(morePostsBtn);
      }
      stageEl.appendChild(section);
    }
    } catch (err) {
      if (seq !== subtabSeq) return;
      renderStageError(err);
    }
  }

  // ------------------------------------------------------------------------
  // Subtab 3: Inner Circle (Cross-platform People + Group Chats)
  // ------------------------------------------------------------------------
  async function renderInnerCircle() {
    const seq = ++subtabSeq;
    clear(stageEl);
    stageEl.appendChild(skeletonCards(3));

    try {
      const data = await api.memoriesPeople({ limit: 30 });
      if (seq !== subtabSeq) return;
      clear(stageEl);

    if (data.people && data.people.length > 0) {
      stageEl.appendChild(
        el(
          "section",
          { className: "mem-section" },
          el(
            "div",
            { className: "mem-section-head" },
            el("h3", { className: "mem-h3" }, "14년을 함께 채워준 사람들 (IG + FB 통합)"),
            el("span", { className: "mem-date-muted" }, "대화 · 댓글 · 좋아요 합산 순")
          ),
          el(
            "div",
            { className: "mem-people-grid" },
            data.people.map((p) => {
              const span = formatYearSpan(p.first_ts, p.last_ts);
              return el(
                "div",
                { className: "card mem-person-card" },
                el(
                  "div",
                  { className: "mem-person-top" },
                  avatarEl(p.avatar_local_path, p.display_name, "md"),
                  el(
                    "div",
                    { className: "mem-person-ident" },
                    el(
                      "div",
                      { className: "mem-person-name-row" },
                      el("strong", null, p.display_name),
                      ...(p.platforms || []).map((pl) => platformTag(pl))
                    ),
                    el(
                      "div",
                      { className: "mem-date-muted tabular" },
                      [p.username ? `@${p.username}` : null, span || null].filter(Boolean).join(" · ")
                    )
                  )
                ),
                el(
                  "div",
                  { className: "mem-person-stats tabular" },
                  p.message_count > 0 ? el("span", { className: "meta-chip" }, `메시지 ${p.message_count.toLocaleString()}`) : null,
                  p.comment_count > 0 ? el("span", { className: "meta-chip" }, `댓글 ${p.comment_count.toLocaleString()}`) : null,
                  p.like_count > 0 ? el("span", { className: "meta-chip" }, `좋아요 ${p.like_count.toLocaleString()}`) : null
                ),
                el(
                  "div",
                  { className: "mem-person-actions" },
                  el(
                    "button",
                    {
                      type: "button",
                      className: "btn btn-sm",
                      onClick: () => ctx.openPerson(p.identifier),
                    },
                    "활동 모아보기"
                  ),
                  p.primary_thread_id
                    ? el(
                        "button",
                        {
                          type: "button",
                          className: "btn btn-ghost btn-sm",
                          onClick: () => ctx.navigateThread(p.primary_thread_id),
                        },
                        "대화방 열기"
                      )
                    : null
                )
              );
            })
          )
        )
      );
    }

    if (data.groups && data.groups.length > 0) {
      stageEl.appendChild(
        el(
          "section",
          { className: "mem-section" },
          el("h3", { className: "mem-h3", style: "margin-bottom:12px;" }, "함께한 단톡방"),
          el(
            "div",
            { className: "mem-people-grid" },
            data.groups.map((g) => {
              const span = formatYearSpan(g.first_ts, g.last_ts);
              return el(
                "button",
                {
                  type: "button",
                  className: "card mem-group-card",
                  onClick: () => ctx.navigateThread(g.thread_id),
                },
                el(
                  "div",
                  { className: "mem-person-top" },
                  avatarEl(g.avatar_local_path, g.title, "md"),
                  el(
                    "div",
                    { className: "mem-person-ident" },
                    el(
                      "div",
                      { className: "mem-person-name-row" },
                      el("strong", null, g.title),
                      platformTag(g.platform)
                    ),
                    el(
                      "div",
                      { className: "mem-date-muted tabular" },
                      [`메시지 ${g.message_count.toLocaleString()}건`, span || null].filter(Boolean).join(" · ")
                    )
                  )
                )
              );
            })
          )
        )
      );
    }
    } catch (err) {
      if (seq !== subtabSeq) return;
      renderStageError(err);
    }
  }

  // ------------------------------------------------------------------------
  // Subtab 4: Unified Photo Wall + Places Chips + LocalStorage Keepsakes
  // ------------------------------------------------------------------------
  async function renderPhotoWall(reset = true) {
    const seq = ++subtabSeq;
    if (reset) {
      state.photoOffset = 0;
      state.photoItems = [];
      photoGridEl = null;
      photoLbMedia = [];
      photoMoreBtn = null;
      clear(stageEl);
      stageEl.appendChild(skeletonGrid(12));
    } else if (photoMoreBtn) {
      photoMoreBtn.disabled = true;
    }

    try {
      let res;
      if (state.photoFavoritesOnly && keepsakes.size === 0) {
        res = { total: 0, items: [], places: [], years: [] };
      } else {
        const queryParams = {
          year: state.photoYear || undefined,
          platform: state.photoPlatform || undefined,
          place: state.photoPlace || undefined,
          sort: state.photoSort,
          offset: state.photoOffset,
          limit: state.photoLimit,
        };
        if (state.photoFavoritesOnly) {
          queryParams.media_ids = Array.from(keepsakes).join(",");
        }
        res = await api.memoriesPhotos(queryParams);
      }
      if (seq !== subtabSeq) return;

      state.photoTotal = res.total;
      if (res.places && res.places.length > 0) {
        state.photoPlaces = res.places;
      }
      if (res.years && res.years.length > 0) {
        state.photoYears = res.years;
      }

      const handleKeepsakeChange = () => {
        if (photoFavBtn) {
          clear(photoFavBtn);
          photoFavBtn.append(
            icon(state.photoFavoritesOnly ? "starFilled" : "star", 14),
            `즐겨찾기 (${keepsakes.size})`
          );
        }
        if (state.photoFavoritesOnly) {
          renderPhotoWall(true);
        }
      };

      if (!reset && photoGridEl) {
        const startIdx = state.photoItems.length;
        state.photoItems.push(...res.items);
        for (const p of res.items) {
          photoLbMedia.push({ local_path: p.local_path, media_type: p.media_type });
        }
        res.items.forEach((item, i) => {
          photoGridEl.appendChild(makePhotoTile(item, photoLbMedia, startIdx + i, handleKeepsakeChange));
        });
        if (photoMoreBtn) {
          if (state.photoSort === "random" || state.photoItems.length >= state.photoTotal || res.items.length === 0) {
            photoMoreBtn.remove();
            photoMoreBtn = null;
          } else {
            photoMoreBtn.disabled = false;
            photoMoreBtn.textContent = `더 보기 (${state.photoItems.length.toLocaleString()} / ${state.photoTotal.toLocaleString()})`;
          }
        }
        return;
      }

      state.photoItems = [...res.items];
      clear(stageEl);

      const yearSelect = el(
        "select",
        { className: "select tabular", "aria-label": "연도 필터" },
        el("option", { value: "", selected: !state.photoYear }, "전체 연도"),
        state.photoYears.map((y) =>
          el("option", { value: y, selected: state.photoYear === y }, `${y}년`)
        )
      );
      yearSelect.addEventListener("change", () => {
        state.photoYear = yearSelect.value ? Number(yearSelect.value) : null;
        renderPhotoWall(true);
      });

      const platSelect = el(
        "select",
        { className: "select", "aria-label": "플랫폼 필터" },
        el("option", { value: "", selected: state.photoPlatform === "" }, "전체 (IG + FB)"),
        el("option", { value: "instagram", selected: state.photoPlatform === "instagram" }, "Instagram"),
        el("option", { value: "facebook", selected: state.photoPlatform === "facebook" }, "Facebook")
      );
      platSelect.addEventListener("change", () => {
        state.photoPlatform = platSelect.value;
        renderPhotoWall(true);
      });

      const sortSelect = el(
        "select",
        { className: "select", "aria-label": "정렬" },
        el("option", { value: "recent", selected: state.photoSort === "recent" }, "최신순"),
        el("option", { value: "oldest", selected: state.photoSort === "oldest" }, "오래된순"),
        el("option", { value: "random", selected: state.photoSort === "random" }, "랜덤 셔플")
      );
      sortSelect.addEventListener("change", () => {
        state.photoSort = sortSelect.value;
        renderPhotoWall(true);
      });

      const favBtn = el(
        "button",
        {
          type: "button",
          className: `btn btn-sm${state.photoFavoritesOnly ? " btn-primary" : ""}`,
          onClick: () => {
            state.photoFavoritesOnly = !state.photoFavoritesOnly;
            renderPhotoWall(true);
          },
        },
        icon(state.photoFavoritesOnly ? "starFilled" : "star", 14),
        `즐겨찾기 (${keepsakes.size})`
      );
      photoFavBtn = favBtn;

      const shuffleBtn =
        state.photoSort === "random"
          ? el(
              "button",
              {
                type: "button",
                className: "btn btn-ghost btn-sm",
                onClick: () => renderPhotoWall(true),
              },
              icon("shuffle", 14),
              "다시 섞기"
            )
          : null;

      const toolbar = el(
        "div",
        { className: "card mem-photo-toolbar" },
        el(
          "div",
          { className: "mem-photo-controls" },
          yearSelect,
          platSelect,
          sortSelect,
          shuffleBtn,
          favBtn,
          el(
            "span",
            { className: "mem-date-muted tabular", style: "margin-left:auto;" },
            `총 ${state.photoTotal.toLocaleString()}장`
          )
        ),
        state.photoPlaces.length > 0
          ? el(
              "div",
              { className: "mem-places-row" },
              el("span", { className: "mem-places-label" }, icon("pin", 13), "장소"),
              state.photoPlace
                ? el(
                    "button",
                    {
                      type: "button",
                      className: "mem-place-chip active",
                      onClick: () => {
                        state.photoPlace = "";
                        renderPhotoWall(true);
                      },
                    },
                    state.photoPlace,
                    " ✕"
                  )
                : null,
              state.photoPlaces
                .filter((pl) => pl.name !== state.photoPlace)
                .slice(0, 18)
                .map((pl) =>
                  el(
                    "button",
                    {
                      type: "button",
                      className: "mem-place-chip",
                      onClick: () => {
                        state.photoPlace = pl.name;
                        renderPhotoWall(true);
                      },
                    },
                    pl.name,
                    el("span", { className: "tabular mem-place-cnt" }, pl.count)
                  )
                )
            )
          : null
      );
      stageEl.appendChild(toolbar);

      if (state.photoItems.length === 0) {
        stageEl.appendChild(
          el(
            "div",
            { className: "empty-box", style: "margin-top:14px;" },
            state.photoFavoritesOnly
              ? "아직 즐겨찾기에 담은 사진이 없습니다. 사진 우측 상단의 ★ 버튼을 눌러 나만의 보물창고를 만들어 보세요."
              : "선택한 조건에 해당하는 사진/영상이 없습니다."
          )
        );
        return;
      }

      photoLbMedia = state.photoItems.map((p) => ({
        local_path: p.local_path,
        media_type: p.media_type,
      }));

      photoGridEl = el(
        "div",
        { className: "mem-photo-grid" },
        state.photoItems.map((item, idx) =>
          makePhotoTile(item, photoLbMedia, idx, handleKeepsakeChange)
        )
      );
      stageEl.appendChild(photoGridEl);

      if (state.photoSort !== "random" && state.photoItems.length < state.photoTotal) {
        photoMoreBtn = el(
          "button",
          {
            type: "button",
            className: "btn",
            style: "display:block;margin:20px auto 0;",
            onClick: () => {
              state.photoOffset += state.photoLimit;
              renderPhotoWall(false);
            },
          },
          `더 보기 (${state.photoItems.length.toLocaleString()} / ${state.photoTotal.toLocaleString()})`
        );
        stageEl.appendChild(photoMoreBtn);
      }
    } catch (err) {
      if (seq !== subtabSeq) return;
      if (reset) {
        renderStageError(err);
      } else if (photoMoreBtn) {
        photoMoreBtn.disabled = false;
        photoMoreBtn.textContent = `다시 시도 (${err.message})`;
      }
    }
  }

  function renderActiveSubtab() {
    if (state.subtab === "yearbook") {
      renderYearbook();
    } else if (state.subtab === "people") {
      renderInnerCircle();
    } else if (state.subtab === "photos") {
      renderPhotoWall(true);
    } else {
      renderOnThisDay(false);
    }
  }

  renderActiveSubtab();
}
