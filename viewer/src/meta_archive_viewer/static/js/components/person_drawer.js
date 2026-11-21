/** Slide-over People activity drawer showing connections, comments, likes, and DMs for a person. */

import { api } from "../api.js";
import { avatarEl, clear, el, formatDate } from "../dom.js";

export function createPersonDrawer(hostEl, { onOpenPost, onNavigateThread }) {
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

  async function open(identifier) {
    if (!identifier) return;
    clear(hostEl);
    const panel = el("aside", { className: "drawer-panel" }, el("div", { className: "loading-box" }, "인물 활동 불러오는 중..."));
    hostEl.appendChild(panel);
    hostEl.classList.remove("hidden");

    try {
      const data = await api.person(identifier, 50);
      clear(panel);

      const firstConn = data.connections && data.connections[0];
      const avatarPath = firstConn ? firstConn.avatar_local_path : null;

      panel.appendChild(
        el(
          "div",
          { className: "modal-header" },
          el(
            "div",
            { style: "display:flex;align-items:center;gap:10px;" },
            avatarEl(avatarPath, data.identifier),
            el(
              "div",
              null,
              el("strong", null, data.identifier),
              el(
                "div",
                { style: "font-size:0.8rem;color:var(--text-muted);" },
                `댓글 ${data.comments.length} · 좋아요 ${data.liked_posts.length} · DM ${data.messages.length}`
              )
            )
          ),
          el("button", { type: "button", className: "btn", onClick: close }, "닫기")
        )
      );

      if (data.connections.length > 0) {
        panel.appendChild(el("h4", { style: "margin:10px 0 6px;" }, "관계 정보"));
        panel.appendChild(
          el(
            "div",
            { className: "post-meta-row" },
            data.connections.map((c) =>
              el(
                "span",
                { className: "pill pill-accent" },
                `${c.platform} · ${c.relation}${c.full_name ? ` (${c.full_name})` : ""}`
              )
            )
          )
        );
      }

      panel.appendChild(el("h4", { style: "margin:14px 0 6px;" }, `남긴 댓글 (${data.comments.length})`));
      if (data.comments.length === 0) {
        panel.appendChild(el("div", { className: "pill" }, "없음"));
      } else {
        panel.appendChild(
          el(
            "div",
            { className: "feed-list" },
            data.comments.map((c) =>
              el(
                "div",
                { className: "card", style: "padding:10px;" },
                el(
                  "div",
                  { style: "font-size:0.78rem;color:var(--text-muted);" },
                  `${c.platform} · ${formatDate(c.created_at, c.timestamp_unix)}`
                ),
                el("div", { style: "margin:4px 0;" }, c.text || "(내용 없음)"),
                el(
                  "button",
                  {
                    type: "button",
                    className: "btn-link",
                    style: "font-size:0.82rem;",
                    onClick: () => {
                      close();
                      onOpenPost(c.post_id);
                    },
                  },
                  `원글 보기${c.post_caption ? `: ${c.post_caption.slice(0, 40)}` : ""}`
                )
              )
            )
          )
        );
      }

      panel.appendChild(el("h4", { style: "margin:14px 0 6px;" }, `좋아요 누른 글 (${data.liked_posts.length})`));
      if (data.liked_posts.length === 0) {
        panel.appendChild(el("div", { className: "pill" }, "없음"));
      } else {
        panel.appendChild(
          el(
            "div",
            { className: "feed-list" },
            data.liked_posts.map((lp) =>
              el(
                "button",
                {
                  type: "button",
                  className: "card",
                  style: "padding:10px;text-align:left;cursor:pointer;",
                  onClick: () => {
                    close();
                    onOpenPost(lp.post_id);
                  },
                },
                el("div", { style: "font-size:0.78rem;color:var(--text-muted);" }, `${lp.platform} · ${formatDate(lp.created_at, lp.timestamp_unix)}`),
                el("div", null, lp.caption ? lp.caption.slice(0, 70) : lp.post_id)
              )
            )
          )
        );
      }

      panel.appendChild(el("h4", { style: "margin:14px 0 6px;" }, `메시지 (${data.messages.length})`));
      if (data.messages.length === 0) {
        panel.appendChild(el("div", { className: "pill" }, "없음"));
      } else {
        panel.appendChild(
          el(
            "div",
            { className: "feed-list" },
            data.messages.map((m) =>
              el(
                "button",
                {
                  type: "button",
                  className: "card",
                  style: "padding:10px;text-align:left;cursor:pointer;",
                  onClick: () => {
                    close();
                    onNavigateThread(m.thread_id);
                  },
                },
                el(
                  "div",
                  { style: "font-size:0.78rem;color:var(--text-muted);" },
                  `${m.thread_title || m.thread_id} · ${formatDate(m.created_at, m.timestamp_unix)}`
                ),
                el("div", null, m.text || "(미디어/첨부)")
              )
            )
          )
        );
      }
    } catch (err) {
      clear(panel);
      panel.appendChild(el("div", { className: "error-box" }, `인물 정보 조회 실패: ${err.message}`));
    }
  }

  return { open, close };
}
