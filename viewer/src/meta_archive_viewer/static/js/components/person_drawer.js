/** Slide-over People activity drawer with tabbed comments, likes, and DMs (direct timestamp jump). */

import { api } from "../api.js?v=2";
import { avatarEl, clear, el, formatDate } from "../dom.js?v=2";

export function createPersonDrawer(hostEl, { onOpenPost, onNavigateThread }) {
  function close() {
    clear(hostEl);
    hostEl.classList.add("hidden");
  }

  hostEl.addEventListener("click", (ev) => {
    if (ev.target === hostEl) close();
  });

  async function open(identifier) {
    if (!identifier) return;
    clear(hostEl);
    const panel = el("aside", { className: "drawer-panel" }, el("div", { className: "loading-box" }, "인물 활동 불러오는 중..."));
    hostEl.appendChild(panel);
    hostEl.classList.remove("hidden");

    try {
      const data = await api.person(identifier, 60);
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
              el("strong", { style: "font-size:1.05rem;" }, data.identifier),
              el(
                "div",
                { style: "font-size:0.79rem;color:var(--text-muted);" },
                `댓글 ${data.comments.length} · 좋아요 ${data.liked_posts.length} · DM ${data.messages.length}`
              )
            )
          ),
          el("button", { type: "button", className: "btn btn-sm", onClick: close }, "✕ 닫기")
        )
      );

      if (data.connections.length > 0) {
        panel.appendChild(
          el(
            "div",
            { className: "post-meta-row", style: "margin-top:0;margin-bottom:10px;" },
            data.connections.map((c) =>
              el(
                "span",
                { className: `pill ${c.platform === "instagram" ? "pill-ig" : "pill-fb"}` },
                `${c.platform} · ${c.relation}${c.full_name ? ` (${c.full_name})` : ""}`
              )
            )
          )
        );
      }

      // Default to whichever tab actually has items
      let activeTab =
        data.comments.length > 0
          ? "comments"
          : data.messages.length > 0
            ? "messages"
            : data.liked_posts.length > 0
              ? "likes"
              : "comments";

      const tabsWrap = el("div", { className: "drawer-tabs" });
      const bodyHost = el("div", { className: "feed-list" });
      panel.append(tabsWrap, bodyHost);

      function renderTab() {
        clear(tabsWrap);
        clear(bodyHost);
        const defs = [
          ["comments", `댓글 (${data.comments.length})`],
          ["likes", `좋아요 (${data.liked_posts.length})`],
          ["messages", `메시지 (${data.messages.length})`],
        ];
        for (const [key, label] of defs) {
          tabsWrap.appendChild(
            el(
              "button",
              {
                type: "button",
                className: `btn btn-sm${activeTab === key ? " btn-primary" : ""}`,
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
                { className: "card", style: "padding:11px;" },
                el(
                  "div",
                  { style: "font-size:0.76rem;color:var(--text-muted);" },
                  `${c.platform} · ${formatDate(c.created_at, c.timestamp_unix)}`
                ),
                el("div", { style: "margin:5px 0;font-size:0.9rem;" }, c.text || "(내용 없음)"),
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
                  `원글 보기${c.post_caption ? `: ${c.post_caption.slice(0, 42)}` : ""}`
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
                  className: "card",
                  style: "padding:11px;text-align:left;cursor:pointer;",
                  onClick: () => {
                    close();
                    onOpenPost(lp.post_id);
                  },
                },
                el("div", { style: "font-size:0.76rem;color:var(--text-muted);" }, `${lp.platform} · ${formatDate(lp.created_at, lp.timestamp_unix)}`),
                el("div", { style: "margin-top:4px;font-size:0.9rem;" }, lp.caption ? lp.caption.slice(0, 80) : lp.post_id)
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
                  className: "card",
                  style: "padding:11px;text-align:left;cursor:pointer;",
                  onClick: () => {
                    close();
                    onNavigateThread(m.thread_id, m.timestamp_unix);
                  },
                },
                el(
                  "div",
                  { style: "font-size:0.76rem;color:var(--text-muted);" },
                  `${m.thread_title || m.thread_id} · ${formatDate(m.created_at, m.timestamp_unix)}`
                ),
                el("div", { style: "margin-top:4px;font-size:0.9rem;" }, m.text || "(미디어/첨부)")
              )
            );
          }
        }
      }

      renderTab();
    } catch (err) {
      clear(panel);
      panel.appendChild(el("div", { className: "error-box" }, `인물 정보 조회 실패: ${err.message}`));
    }
  }

  return { open, close };
}
