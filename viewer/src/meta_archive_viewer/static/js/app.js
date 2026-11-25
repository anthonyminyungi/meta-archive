/** Main SPA hash router & shared controllers for Meta Archive viewer. */

import { api } from "./api.js?v=4";
import { clear, el } from "./dom.js?v=3";
import { icon } from "./icons.js?v=4";
import { createLightbox } from "./components/lightbox.js?v=3";
import { createPersonDrawer } from "./components/person_drawer.js?v=3";
import { createPostModal } from "./components/post_modal.js?v=3";
import { renderFacebookView } from "./views/facebook.js?v=3";
import { renderInstagramView } from "./views/instagram.js?v=3";
import { renderMemoriesView } from "./views/memories.js?v=4";
import { renderMessagesView } from "./views/messages.js?v=3";
import { renderSearchView } from "./views/search.js?v=3";

const mainEl = document.getElementById("app-main");
const modalRoot = document.getElementById("modal-root");
const lightboxRoot = document.getElementById("lightbox-root");
const drawerRoot = document.getElementById("drawer-root");
const overviewBadge = document.getElementById("overview-badge");
const themeBtn = document.getElementById("theme-toggle");

const lightbox = createLightbox(lightboxRoot);

let postModal = null;
let personDrawer = null;

function navigateThread(threadId, ts = null) {
  const qs = ts
    ? `thread=${encodeURIComponent(threadId)}&ts=${encodeURIComponent(ts)}`
    : `thread=${encodeURIComponent(threadId)}`;
  window.location.hash = `#/messages?${qs}`;
}

personDrawer = createPersonDrawer(drawerRoot, {
  onOpenPost: (postId) => postModal.open(postId),
  onNavigateThread: navigateThread,
});

postModal = createPostModal(modalRoot, {
  onOpenLightbox: (mediaList, idx) => lightbox.open(mediaList, idx),
  onOpenPerson: (ident) => personDrawer.open(ident),
  isLightboxOpen: () => !lightboxRoot.classList.contains("hidden"),
});

// Centralized Escape priority: Lightbox -> Drawer -> Modal
window.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") {
    if (!lightboxRoot.classList.contains("hidden")) {
      lightbox.close();
      return;
    }
    if (!drawerRoot.classList.contains("hidden")) {
      personDrawer.close();
      return;
    }
    if (!modalRoot.classList.contains("hidden")) {
      postModal.close();
      return;
    }
  } else if (ev.key === "/" && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") {
      ev.preventDefault();
      if (!window.location.hash.startsWith("#/search")) {
        window.location.hash = "#/search";
      } else {
        const searchInput = mainEl.querySelector('input[type="search"]');
        if (searchInput) searchInput.focus();
      }
    }
  }
});

const ctx = {
  modal: postModal,
  openPost: (postId) => postModal.open(postId),
  openLightbox: (mediaList, idx) => lightbox.open(mediaList, idx),
  openPerson: (ident) => personDrawer.open(ident),
  navigateThread,
};

function currentEffectiveTheme() {
  const explicit = document.documentElement.dataset.theme;
  if (explicit === "dark" || explicit === "light") return explicit;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function updateThemeButton() {
  const isDark = currentEffectiveTheme() === "dark";
  clear(themeBtn);
  themeBtn.appendChild(icon(isDark ? "sun" : "moon", 16));
  themeBtn.title = isDark ? "라이트 모드로 전환" : "다크 모드로 전환";
  themeBtn.setAttribute("aria-label", themeBtn.title);
}

function initTheme() {
  const saved = localStorage.getItem("meta-archive-theme");
  if (saved === "dark" || saved === "light") {
    document.documentElement.dataset.theme = saved;
  }
  updateThemeButton();
  themeBtn.addEventListener("click", () => {
    const next = currentEffectiveTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("meta-archive-theme", next);
    updateThemeButton();
  });
}

function parseHash() {
  const raw = (window.location.hash || "#/memories").replace(/^#\/?/, "");
  const [routePart, qs] = raw.split("?", 2);
  const route = routePart || "memories";
  const params = new URLSearchParams(qs || "");
  return { route, params };
}

function setActiveTab(route) {
  for (const tab of document.querySelectorAll(".nav-tab")) {
    tab.classList.toggle("active", tab.dataset.route === route);
  }
}

async function refreshHeaderStats() {
  try {
    const ov = await api.overview();
    const t = ov.totals || {};
    overviewBadge.textContent = `${(t.posts || 0).toLocaleString()} 게시물 · ${(t.stories || 0).toLocaleString()} 스토리 · ${(t.messages || 0).toLocaleString()} 메시지`;
  } catch (err) {
    overviewBadge.textContent = `오프라인 (${err.message})`;
  }
}

async function routeNow() {
  const { route, params } = parseHash();
  setActiveTab(route);

  try {
    if (route === "instagram") {
      await renderInstagramView(mainEl, ctx);
    } else if (route === "facebook") {
      await renderFacebookView(mainEl, ctx);
    } else if (route === "messages") {
      await renderMessagesView(mainEl, ctx, params.get("thread"), params.get("ts"));
    } else if (route === "search") {
      await renderSearchView(mainEl, ctx, params.get("q") || "");
    } else {
      setActiveTab("memories");
      await renderMemoriesView(mainEl, ctx, params);
    }
  } catch (err) {
    clear(mainEl);
    mainEl.appendChild(
      el(
        "div",
        { className: "error-box" },
        el("strong", null, "아카이브를 불러올 수 없습니다."),
        el("div", { style: "margin-top:8px;" }, err.message),
        el(
          "div",
          { style: "margin-top:8px;font-size:0.84rem;color:var(--text-muted);" },
          "ARCHIVE_DIR 경로와 archive/archive.sqlite 생성 여부를 확인하세요."
        )
      )
    );
  }
}

initTheme();
window.addEventListener("hashchange", routeNow);
refreshHeaderStats();
routeNow();
