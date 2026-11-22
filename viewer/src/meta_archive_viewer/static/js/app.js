/** Main SPA hash router & shared controllers for Meta Archive viewer. */

import { api } from "./api.js";
import { clear, el } from "./dom.js";
import { createLightbox } from "./components/lightbox.js";
import { createPersonDrawer } from "./components/person_drawer.js";
import { createPostModal } from "./components/post_modal.js";
import { renderFacebookView } from "./views/facebook.js";
import { renderInstagramView } from "./views/instagram.js";
import { renderMessagesView } from "./views/messages.js";
import { renderSearchView } from "./views/search.js";

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
  const qs = ts ? `thread=${encodeURIComponent(threadId)}&ts=${encodeURIComponent(ts)}` : `thread=${encodeURIComponent(threadId)}`;
  window.location.hash = `#/messages?${qs}`;
}

personDrawer = createPersonDrawer(drawerRoot, {
  onOpenPost: (postId) => postModal.open(postId),
  onNavigateThread: navigateThread,
});

postModal = createPostModal(modalRoot, {
  onOpenLightbox: (mediaList, idx) => lightbox.open(mediaList, idx),
  onOpenPerson: (ident) => personDrawer.open(ident),
});

const ctx = {
  modal: postModal,
  openPost: (postId) => postModal.open(postId),
  openLightbox: (mediaList, idx) => lightbox.open(mediaList, idx),
  openPerson: (ident) => personDrawer.open(ident),
  navigateThread,
};

function initTheme() {
  const saved = localStorage.getItem("meta-archive-theme");
  if (saved === "dark" || saved === "light") {
    document.documentElement.dataset.theme = saved;
  }
  themeBtn.addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme;
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("meta-archive-theme", next);
  });
}

function parseHash() {
  const raw = (window.location.hash || "#/instagram").replace(/^#\/?/, "");
  const [routePart, qs] = raw.split("?", 2);
  const route = routePart || "instagram";
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
    overviewBadge.textContent = `게시물 ${t.posts || 0} · 스토리 ${t.stories || 0} · 메시지 ${t.messages || 0}`;
  } catch (err) {
    overviewBadge.textContent = `오프라인 (${err.message})`;
  }
}

async function routeNow() {
  const { route, params } = parseHash();
  setActiveTab(route);

  try {
    if (route === "facebook") {
      await renderFacebookView(mainEl, ctx);
    } else if (route === "messages") {
      await renderMessagesView(mainEl, ctx, params.get("thread"), params.get("ts"));
    } else if (route === "search") {
      await renderSearchView(mainEl, ctx, params.get("q") || "");
    } else {
      setActiveTab("instagram");
      await renderInstagramView(mainEl, ctx);
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
          { style: "margin-top:8px;font-size:0.85rem;color:var(--text-muted);" },
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
