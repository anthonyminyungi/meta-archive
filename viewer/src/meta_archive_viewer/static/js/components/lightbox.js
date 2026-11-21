/** Full-screen media lightbox overlay. */

import { mediaUrl } from "../api.js";
import { clear, el, isVideoMedia } from "../dom.js";

export function createLightbox(hostEl) {
  function close() {
    clear(hostEl);
    hostEl.classList.add("hidden");
  }

  hostEl.addEventListener("click", (ev) => {
    if (ev.target === hostEl) close();
  });

  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !hostEl.classList.contains("hidden")) {
      close();
    }
  });

  function open(mediaList, startIndex = 0) {
    if (!mediaList || mediaList.length === 0) return;
    let idx = Math.max(0, Math.min(startIndex, mediaList.length - 1));

    const stage = el("div", { className: "lightbox-stage" });

    function render() {
      clear(stage);
      const item = mediaList[idx];
      const src = mediaUrl(item.local_path);

      const topBar = el(
        "div",
        { style: "display:flex;gap:12px;align-items:center;color:#fff;" },
        mediaList.length > 1
          ? el("span", null, `${idx + 1} / ${mediaList.length}`)
          : null,
        item.taken_at_exif ? el("span", null, `EXIF: ${item.taken_at_exif}`) : null,
        el(
          "button",
          {
            type: "button",
            className: "btn",
            style: "background:#222;color:#fff;border-color:#444;",
            onClick: close,
          },
          "닫기 (ESC)"
        )
      );

      let mediaNode = el("div", { className: "empty-box" }, "미디어 파일 경로 없음");
      if (src) {
        if (isVideoMedia(item)) {
          mediaNode = el("video", {
            src,
            controls: true,
            autoplay: true,
            playsinline: true,
            preload: "metadata",
          });
        } else {
          mediaNode = el("img", { src, alt: item.media_id || "archived media" });
        }
      }

      const navRow =
        mediaList.length > 1
          ? el(
              "div",
              { style: "display:flex;gap:10px;" },
              el(
                "button",
                {
                  type: "button",
                  className: "btn",
                  onClick: () => {
                    idx = (idx - 1 + mediaList.length) % mediaList.length;
                    render();
                  },
                },
                "◀ 이전"
              ),
              el(
                "button",
                {
                  type: "button",
                  className: "btn",
                  onClick: () => {
                    idx = (idx + 1) % mediaList.length;
                    render();
                  },
                },
                "다음 ▶"
              )
            )
          : null;

      stage.appendChild(topBar);
      stage.appendChild(mediaNode);
      if (navRow) stage.appendChild(navRow);
    }

    clear(hostEl);
    hostEl.appendChild(stage);
    hostEl.classList.remove("hidden");
    render();
  }

  return { open, close };
}
