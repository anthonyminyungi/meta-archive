/** Slide-over People activity drawer with tabbed comments, likes, and DMs (direct timestamp jump). */

import { api } from "../api.js?v=5";
import { avatarEl, clear, el, formatDate, skeletonCards } from "../dom.js?v=5";
import { icon } from "../icons.js?v=5";

export function createPersonDrawer(hostEl, { onOpenPost, onNavigateThread }) {
  let openSeq = 0;

  function close() {
    openSeq += 1;
    clear(hostEl);
    hostEl.classList.add("hidden");
  }

  hostEl.addEventListener("click", (ev) => {
    if (ev.target === hostEl) close();
  });

  async function open(identifier) {
    if (!identifier) return;
    const seq = ++openSeq;
    clear(hostEl);
    const panel = el("aside", { className: "drawer-panel" }, skeletonCards(3));
    hostEl.appendChild(panel);
    hostEl.classList.remove("hidden");

    try {
      const data = await api.person(identifier, 60);
      if (seq !== openSeq) return;
      clear(panel);

      const firstConn = data.connections && data.connections[0];
      const avatarPath = data.avatar_local_path || (firstConn ? firstConn.avatar_local_path : null);

      panel.appendChild(
        el(
          "div",
          { className: "modal-header" },
          el(
            "div",
            { style: "display:flex;align-items:center;gap:11px;min-width:0;" },
            avatarEl(avatarPath, data.identifier, "md"),
            el(
              "div",
              { style: "min-width:0;" },
              el("strong", { style: "font-size:1.02rem;letter-spacing:-0.015em;" }, data.identifier),
              el(
                "div",
                { className: "tabular", style: "font-size:0.78rem;color:var(--text-muted);" },
                `댓글 ${data.comments.length} · 좋아요 ${data.liked_posts.length} · DM ${data.messages.length}`
              )
            )
          ),
          el(
            "button",
            { type: "button", className: "icon-btn", onClick: close, "aria-label": "닫기", title: "닫기 (ESC)" },
            icon("close", 17)
          )
        )
      );

      if (data.connections.length > 0) {
        panel.appendChild(
          el(
            "div",
            { className: "post-meta-row", style: "margin-top:0;margin-bottom:12px;" },
            data.connections.map((c) =>
              el(
                "span",
                { className: "meta-chip" },
                `${c.platform === "instagram" ? "IG" : "FB"} · ${c.relation}${c.full_name ? ` (${c.full_name})` : ""}`
              )
            )
          )
        );
      }

      let activeTab =
        data.comments.length > 0
          ? "comments"
          : data.messages.length > 0
            ? "messages"
            : data.liked_posts.length > 0
              ? "likes"
              : "comments";

      const tabsWrap = el("div", { className: "segmented-control drawer-tabs" });
      const bodyHost = el("div", { className: "feed-list" });
      panel.append(tabsWrap, bodyHost);

      function renderTab() {
        clear(tabsWrap);
        clear(bodyHost);
        const defs = [
          ["comments", `댓글 ${data.comments.length}`],
          ["likes", `좋아요 ${data.liked_posts.length}`],
          ["messages", `메시지 ${data.messages.length}`],
        ];
        for (const [key, label] of defs) {
          tabsWrap.appendChild(
            el(
              "button",
              {
                type: "button",
                className: `seg-btn${activeTab === key ? " active" : ""}`,
                onClick: () => {
                  activeTab = key;
                  renderTab();
                },
              },
              label
            )
          );
        }

        if (activeTab === "comments") {
          if (data.comments.length === 0) {
            bodyHost.appendChild(el("div", { className: "empty-box", style: "padding:18px;" }, "남긴 댓글이 없습니다."));
            return;
          }
          for (const c of data.comments) {
            bodyHost.appendChild(
              el(
                "div",
                { className: "card drawer-item-card" },
                el(
                  "div",
                  { className: "drawer-item-meta" },
                  `${c.platform === "instagram" ? "Instagram" : "Facebook"} · ${formatDate(c.created_at, c.timestamp_unix)}`
                ),
                el("div", { className: "drawer-item-body" }, c.text || "(내용 없음)"),
                el(
                  "button",
                  {
                    type: "button",
                    className: "btn-link",
                    style: "font-size:0.8rem;",
                    onClick: () => {
                      close();
                      onOpenPost(c.post_id);
                    },
                  },
                  `원글 보기${c.post_caption ? ` · ${c.post_caption.slice(0, 40)}` : ""}`
                )
              )
            );
          }
        } else if (activeTab === "likes") {
          if (data.liked_posts.length === 0) {
            bodyHost.appendChild(el("div", { className: "empty-box", style: "padding:18px;" }, "좋아요한 글이 없습니다."));
            return;
          }
          for (const lp of data.liked_posts) {
            bodyHost.appendChild(
              el(
                "button",
                {
                  type: "button",
                  className: "card drawer-item-card drawer-item-btn",
                  onClick: () => {
                    close();
                    onOpenPost(lp.post_id);
                  },
                },
                el("div", { className: "drawer-item-meta" }, `${lp.platform === "instagram" ? "Instagram" : "Facebook"} · ${formatDate(lp.created_at, lp.timestamp_unix)}`),
                el("div", { className: "drawer-item-body" }, lp.caption ? lp.caption.slice(0, 84) : lp.post_id)
              )
            );
          }
        } else {
          if (data.messages.length === 0) {
            bodyHost.appendChild(el("div", { className: "empty-box", style: "padding:18px;" }, "메시지 기록이 없습니다."));
            return;
          }
          for (const m of data.messages) {
            bodyHost.appendChild(
              el(
                "button",
                {
                  type: "button",
                  className: "card drawer-item-card drawer-item-btn",
                  onClick: () => {
                    close();
                    onNavigateThread(m.thread_id, m.timestamp_unix);
                  },
                },
                el(
                  "div",
                  { className: "drawer-item-meta" },
                  `${m.thread_title || m.thread_id} · ${formatDate(m.created_at, m.timestamp_unix)}`
                ),
                el("div", { className: "drawer-item-body" }, m.text || "(미디어/첨부)")
              )
            );
          }
        }
      }

      renderTab();
    } catch (err) {
      if (seq !== openSeq) return;
      clear(panel);
      panel.appendChild(el("div", { className: "error-box" }, `인물 정보 조회 실패: ${err.message}`));
    }
  }

  return { open, close };
}
