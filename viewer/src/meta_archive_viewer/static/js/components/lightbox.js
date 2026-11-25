/** Zero-flicker full-screen media lightbox with persistent chrome, decode() swap, and keyboard navigation. */

import { mediaUrl, preloadImage } from "../api.js?v=5";
import { clear, el, isAudioMedia, isDocumentMedia, isVideoMedia } from "../dom.js?v=5";
import { icon } from "../icons.js?v=5";

function isPlainImage(item) {
  return Boolean(item && item.local_path && !isAudioMedia(item) && !isVideoMedia(item) && !isDocumentMedia(item));
}

export function createLightbox(hostEl) {
  let activeNav = null;

  function close() {
    activeNav = null;
    clear(hostEl);
    hostEl.classList.add("hidden");
  }

  hostEl.addEventListener("click", (ev) => {
    if (ev.target === hostEl) close();
  });

  window.addEventListener("keydown", (ev) => {
    if (hostEl.classList.contains("hidden") || !activeNav) return;
    if (ev.key === "ArrowLeft") {
      ev.preventDefault();
      activeNav(-1);
    } else if (ev.key === "ArrowRight") {
      ev.preventDefault();
      activeNav(1);
    }
  });

  function open(mediaList, startIndex = 0) {
    if (!mediaList || mediaList.length === 0) return;
    let idx = Math.max(0, Math.min(startIndex, mediaList.length - 1));
    let renderSeq = 0;
    let activeImg = null;

    const stage = el("div", { className: "lightbox-stage" });
    const counterEl = mediaList.length > 1 ? el("span", { className: "lightbox-counter" }, "") : null;
    const exifEl = el("span", { className: "lightbox-exif" }, "");
    const closeBtn = el(
      "button",
      { type: "button", className: "lightbox-btn", onClick: close, "aria-label": "닫기" },
      icon("close", 16),
      el("span", null, "ESC")
    );
    const topBar = el("div", { className: "lightbox-topbar" }, counterEl, exifEl, closeBtn);
    const mediaSlot = el("div", { className: "lightbox-media-slot" });

    function preloadNeighbors(curIdx) {
      if (mediaList.length <= 1) return;
      const nextIdx = (curIdx + 1) % mediaList.length;
      const prevIdx = (curIdx - 1 + mediaList.length) % mediaList.length;
      if (isPlainImage(mediaList[nextIdx])) preloadImage(mediaUrl(mediaList[nextIdx].local_path));
      if (isPlainImage(mediaList[prevIdx])) preloadImage(mediaUrl(mediaList[prevIdx].local_path));
    }

    function render({ initial = false } = {}) {
      const seq = ++renderSeq;
      const item = mediaList[idx];
      const src = mediaUrl(item.local_path);
      if (counterEl) counterEl.textContent = `${idx + 1} / ${mediaList.length}`;
      exifEl.textContent = item.taken_at_exif ? `EXIF ${item.taken_at_exif}` : "";
      preloadNeighbors(idx);

      if (!src) {
        activeImg = null;
        clear(mediaSlot);
        mediaSlot.appendChild(el("div", { className: "empty-box" }, "미디어 파일 경로 없음"));
        return;
      }

      if (isPlainImage(item)) {
        const applyImg = () => {
          if (seq !== renderSeq) return;
          if (activeImg && activeImg.parentNode === mediaSlot) {
            activeImg.src = src;
            activeImg.alt = item.media_id || "archived media";
            return;
          }
          clear(mediaSlot);
          const img = el("img", { src, alt: item.media_id || "archived media", decoding: "async" });
          activeImg = img;
          mediaSlot.appendChild(img);
        };
        if (initial) {
          preloadImage(src);
          applyImg();
        } else {
          preloadImage(src).then(applyImg);
        }
        return;
      }

      activeImg = null;
      clear(mediaSlot);
      if (isAudioMedia(item)) {
        mediaSlot.appendChild(el("audio", { src, controls: true, autoplay: true, preload: "metadata" }));
      } else if (isVideoMedia(item)) {
        mediaSlot.appendChild(
          el("video", {
            src,
            controls: true,
            autoplay: true,
            playsinline: true,
            preload: "metadata",
          })
        );
      } else if (isDocumentMedia(item)) {
        const fname = (item.local_path || "첨부파일").split("/").pop();
        mediaSlot.appendChild(
          el(
            "a",
            { className: "lightbox-btn", href: src, target: "_blank", rel: "noopener noreferrer" },
            icon("file", 15),
            `첨부 파일 열기 (${fname})`
          )
        );
      }
    }

    function step(delta) {
      if (mediaList.length <= 1) return;
      idx = (idx + delta + mediaList.length) % mediaList.length;
      render();
    }

    activeNav = step;

    const prevSideBtn =
      mediaList.length > 1
        ? el(
            "button",
            {
              type: "button",
              className: "lightbox-side-nav lightbox-prev",
              "aria-label": "이전 미디어",
              onClick: () => step(-1),
            },
            icon("chevronLeft", 22)
          )
        : null;

    const nextSideBtn =
      mediaList.length > 1
        ? el(
            "button",
            {
              type: "button",
              className: "lightbox-side-nav lightbox-next",
              "aria-label": "다음 미디어",
              onClick: () => step(1),
            },
            icon("chevronRight", 22)
          )
        : null;

    stage.append(topBar, mediaSlot);
    if (prevSideBtn) stage.appendChild(prevSideBtn);
    if (nextSideBtn) stage.appendChild(nextSideBtn);

    clear(hostEl);
    hostEl.appendChild(stage);
    hostEl.classList.remove("hidden");
    render({ initial: true });
  }

  return { open, close };
}
