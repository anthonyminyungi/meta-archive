/** Zero-flicker multi-media carousel with persistent DOM, adjacent preloading, decode() swap, and swipe/keyboard support. */

import { mediaUrl, preloadImage } from "../api.js?v=3";
import { clear, el, isAudioMedia, isDocumentMedia, isVideoMedia } from "../dom.js?v=3";
import { icon } from "../icons.js?v=3";

function isPlainImage(item) {
  return Boolean(item && item.local_path && !isAudioMedia(item) && !isVideoMedia(item) && !isDocumentMedia(item));
}

export function renderCarousel(mediaList, onOpenLightbox, { eager = false } = {}) {
  const playable = (mediaList || []).filter((m) => m && m.local_path && !m.is_carousel_cover);
  const items = playable.length > 0 ? playable : (mediaList || []).filter((m) => m && m.local_path);
  if (items.length === 0) return null;

  let index = 0;
  let renderSeq = 0;
  let activeImg = null;

  const wrap = el("div", { className: "carousel" });
  const stage = el("div", { className: "carousel-stage" });
  const mediaSlot = el("div", { className: "carousel-media-slot" });
  stage.appendChild(mediaSlot);

  let counterEl = null;
  const dotButtons = [];
  let dotsWrap = null;

  function preloadNeighbors(curIdx) {
    if (items.length <= 1) return;
    const neighborIndices = new Set([
      (curIdx + 1) % items.length,
      (curIdx - 1 + items.length) % items.length,
    ]);
    for (const ni of neighborIndices) {
      const candidate = items[ni];
      if (isPlainImage(candidate)) {
        preloadImage(mediaUrl(candidate.local_path));
      }
    }
  }

  function updateChrome() {
    if (counterEl) {
      counterEl.textContent = `${index + 1} / ${items.length}`;
    }
    for (let i = 0; i < dotButtons.length; i += 1) {
      dotButtons[i].classList.toggle("active", i === index);
    }
  }

  function renderSlide({ initial = false } = {}) {
    const seq = ++renderSeq;
    const current = items[index];
    const src = mediaUrl(current.local_path);
    updateChrome();
    preloadNeighbors(index);

    if (!src) {
      activeImg = null;
      clear(mediaSlot);
      return;
    }

    if (isPlainImage(current)) {
      const applyImage = () => {
        if (seq !== renderSeq) return;
        if (activeImg && activeImg.parentNode === mediaSlot) {
          activeImg.src = src;
          activeImg.alt = current.media_id || "post media";
          return;
        }
        clear(mediaSlot);
        const img = el("img", {
          src,
          alt: current.media_id || "post media",
          decoding: "async",
          loading: eager ? "eager" : "lazy",
          onClick: () => onOpenLightbox && onOpenLightbox(items, index),
        });
        activeImg = img;
        mediaSlot.appendChild(img);
      };

      if (initial) {
        preloadImage(src);
        applyImage();
      } else {
        // Keep current image visible until next image is decoded (zero blank flash)
        preloadImage(src).then(applyImage);
      }
      return;
    }

    // Non-image media (video, audio, document)
    activeImg = null;
    clear(mediaSlot);
    if (isAudioMedia(current)) {
      mediaSlot.appendChild(el("audio", { src, controls: true, preload: "metadata" }));
    } else if (isVideoMedia(current)) {
      mediaSlot.appendChild(
        el("video", {
          src,
          controls: true,
          playsinline: true,
          preload: "metadata",
        })
      );
    } else if (isDocumentMedia(current)) {
      const fname = (current.local_path || "첨부파일").split("/").pop();
      mediaSlot.appendChild(
        el(
          "div",
          { style: "padding:24px;text-align:center;" },
          el(
            "a",
            { className: "btn", href: src, target: "_blank", rel: "noopener noreferrer" },
            icon("file", 15),
            `첨부 파일 열기 (${fname})`
          )
        )
      );
    }
  }

  function step(delta) {
    if (items.length <= 1) return;
    index = (index + delta + items.length) % items.length;
    renderSlide();
  }

  if (items.length > 1) {
    counterEl = el("span", { className: "carousel-counter" }, `1 / ${items.length}`);
    const prevBtn = el(
      "button",
      {
        type: "button",
        className: "carousel-nav carousel-prev",
        "aria-label": "이전 미디어",
        onClick: (ev) => {
          ev.stopPropagation();
          step(-1);
        },
      },
      icon("chevronLeft", 18)
    );
    const nextBtn = el(
      "button",
      {
        type: "button",
        className: "carousel-nav carousel-next",
        "aria-label": "다음 미디어",
        onClick: (ev) => {
          ev.stopPropagation();
          step(1);
        },
      },
      icon("chevronRight", 18)
    );
    stage.append(counterEl, prevBtn, nextBtn);

    dotsWrap = el("div", { className: "carousel-dots" });
    items.forEach((_, i) => {
      const dot = el("button", {
        type: "button",
        className: `carousel-dot${i === 0 ? " active" : ""}`,
        "aria-label": `${i + 1}번째 미디어`,
        onClick: (ev) => {
          ev.stopPropagation();
          if (index !== i) {
            index = i;
            renderSlide();
          }
        },
      });
      dotButtons.push(dot);
      dotsWrap.appendChild(dot);
    });

    // Touch swipe support
    let touchStartX = null;
    stage.addEventListener(
      "touchstart",
      (ev) => {
        if (ev.touches && ev.touches.length === 1) {
          touchStartX = ev.touches[0].clientX;
        }
      },
      { passive: true }
    );
    stage.addEventListener(
      "touchend",
      (ev) => {
        if (touchStartX === null || !ev.changedTouches || ev.changedTouches.length === 0) return;
        const dx = ev.changedTouches[0].clientX - touchStartX;
        touchStartX = null;
        if (Math.abs(dx) > 42) {
          step(dx < 0 ? 1 : -1);
        }
      },
      { passive: true }
    );
  }

  wrap.appendChild(stage);
  if (dotsWrap) wrap.appendChild(dotsWrap);
  wrap._carouselStep = step;

  renderSlide({ initial: true });
  return wrap;
}
