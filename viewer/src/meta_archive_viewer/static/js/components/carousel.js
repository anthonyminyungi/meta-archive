/** Multi-media carousel component supporting photos, audio/video playback, and lightbox zoom. */

import { mediaUrl } from "../api.js";
import { clear, el, isAudioMedia, isDocumentMedia, isVideoMedia } from "../dom.js";

export function renderCarousel(mediaList, onOpenLightbox) {
  const playable = (mediaList || []).filter((m) => m && m.local_path && !m.is_carousel_cover);
  const items = playable.length > 0 ? playable : (mediaList || []).filter((m) => m && m.local_path);
  if (items.length === 0) return null;

  let index = 0;
  const wrap = el("div", { className: "carousel" });
  const stage = el("div", { className: "carousel-stage" });
  const dotsWrap = items.length > 1 ? el("div", { className: "carousel-dots" }) : null;

  function renderSlide() {
    clear(stage);
    const current = items[index];
    const src = mediaUrl(current.local_path);
    if (src) {
      if (isAudioMedia(current)) {
        stage.appendChild(el("audio", { src, controls: true, preload: "metadata" }));
      } else if (isVideoMedia(current)) {
        const video = el("video", {
          src,
          controls: true,
          playsinline: true,
          preload: "metadata",
        });
        stage.appendChild(video);
      } else if (isDocumentMedia(current)) {
        const fname = (current.local_path || "첨부파일").split("/").pop();
        stage.appendChild(
          el(
            "div",
            { style: "padding:24px;text-align:center;" },
            el(
              "a",
              {
                className: "btn",
                href: src,
                target: "_blank",
                rel: "noopener noreferrer",
              },
              `📄 첨부 파일 열기 (${fname})`
            )
          )
        );
      } else {
        const img = el("img", {
          src,
          alt: current.media_id || "post media",
          loading: "lazy",
          onClick: () => onOpenLightbox && onOpenLightbox(items, index),
        });
        stage.appendChild(img);
      }
    }

    if (items.length > 1) {
      const prevBtn = el(
        "button",
        {
          type: "button",
          className: "carousel-nav carousel-prev",
          "aria-label": "이전 미디어",
          onClick: (ev) => {
            ev.stopPropagation();
            index = (index - 1 + items.length) % items.length;
            renderSlide();
          },
        },
        "◀"
      );
      const nextBtn = el(
        "button",
        {
          type: "button",
          className: "carousel-nav carousel-next",
          "aria-label": "다음 미디어",
          onClick: (ev) => {
            ev.stopPropagation();
            index = (index + 1) % items.length;
            renderSlide();
          },
        },
        "▶"
      );
      stage.appendChild(prevBtn);
      stage.appendChild(nextBtn);
    }

    if (dotsWrap) {
      clear(dotsWrap);
      items.forEach((_, i) => {
        dotsWrap.appendChild(
          el("button", {
            type: "button",
            className: `carousel-dot${i === index ? " active" : ""}`,
            "aria-label": `${i + 1}번째 미디어`,
            onClick: () => {
              index = i;
              renderSlide();
            },
          })
        );
      });
    }
  }

  wrap.appendChild(stage);
  if (dotsWrap) wrap.appendChild(dotsWrap);
  renderSlide();
  return wrap;
}
