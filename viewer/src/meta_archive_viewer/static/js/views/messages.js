/** Unified IG DM + FB Messenger inbox and thread viewer (supports E2EE threads and attached media). */

import { api, mediaUrl } from "../api.js";
import {
  avatarEl,
  clear,
  el,
  formatDate,
  formatDuration,
  isAudioMedia,
  isDocumentMedia,
  isVideoMedia,
  personButton,
  safeExternalHref,
} from "../dom.js";

export async function renderMessagesView(container, ctx, initialThreadId = null, initialTs = null) {
  clear(container);

  const state = {
    platform: "",
    q: "",
    selectedThreadId: initialThreadId,
    jumpTs: initialTs ? Number(initialTs) : null,
    limit: 100,
    total: 0,
    hasOlder: false,
    hasNewer: false,
    messages: [],
  };

  const platformSelect = el(
    "select",
    { className: "select", "aria-label": "메시지 플랫폼" },
    el("option", { value: "" }, "전체 (IG + FB)"),
    el("option", { value: "instagram" }, "Instagram DM"),
    el("option", { value: "facebook" }, "Facebook Messenger")
  );
  const searchInput = el("input", {
    type: "search",
    className: "input",
    placeholder: "대화방 제목 / 참여자 검색...",
  });

  const threadsListEl = el("div", { className: "thread-list" });
  const chatPane = el("section", { className: "card chat-pane" }, el("div", { className: "empty-box" }, "좌측에서 대화방을 선택하세요."));

  const shell = el(
    "div",
    { className: "messages-shell" },
    el(
      "aside",
      { className: "card threads-pane" },
      el("div", { style: "display:flex;flex-direction:column;gap:8px;" }, platformSelect, searchInput),
      threadsListEl
    ),
    chatPane
  );
  container.appendChild(shell);

  async function loadThreads() {
    clear(threadsListEl);
    threadsListEl.appendChild(el("div", { className: "loading-box" }, "대화 목록 불러오는 중..."));
    const res = await api.threads({
      platform: state.platform || undefined,
      q: state.q || undefined,
      limit: 300,
    });
    clear(threadsListEl);
    if (res.items.length === 0) {
      threadsListEl.appendChild(el("div", { className: "empty-box" }, "저장된 대화방이 없습니다."));
      return;
    }

    if (!state.selectedThreadId) {
      state.selectedThreadId = res.items[0].thread_id;
    }

    for (const t of res.items) {
      const btn = el(
        "button",
        {
          type: "button",
          className: `thread-item${t.thread_id === state.selectedThreadId ? " active" : ""}`,
          onClick: () => {
            state.selectedThreadId = t.thread_id;
            state.jumpTs = null;
            loadThreadsHighlight();
            loadThreadMessages(t.thread_id, "tail");
          },
          dataset: { threadId: t.thread_id },
        },
        el(
          "div",
          { style: "display:flex;justify-content:space-between;gap:6px;align-items:center;" },
          el(
            "div",
            { style: "display:flex;gap:8px;align-items:center;min-width:0;" },
            t.avatar_local_path ? avatarEl(t.avatar_local_path, t.title || t.thread_id) : null,
            el("strong", { style: "font-size:0.93rem;overflow:hidden;text-overflow:ellipsis;" }, t.title || t.thread_id)
          ),
          el("span", { className: "pill" }, t.platform === "instagram" ? "IG" : "FB")
        ),
        el(
          "div",
          { style: "display:flex;gap:6px;flex-wrap:wrap;margin-top:4px;font-size:0.78rem;color:var(--text-muted);" },
          el("span", null, `${t.message_count}건`),
          t.is_e2ee ? el("span", { className: "pill pill-accent" }, "E2EE") : null,
          t.is_pending ? el("span", { className: "pill" }, "요청함") : null
        )
      );
      threadsListEl.appendChild(btn);
    }

    if (state.selectedThreadId) {
      await loadThreadMessages(state.selectedThreadId, state.jumpTs ? "around" : "tail");
    }
  }

  function loadThreadsHighlight() {
    for (const node of threadsListEl.querySelectorAll(".thread-item")) {
      node.classList.toggle("active", node.dataset.threadId === state.selectedThreadId);
    }
  }

  async function loadThreadMessages(threadId, mode = "tail") {
    let queryParams = { limit: state.limit };
    if (mode === "tail") {
      state.messages = [];
      clear(chatPane);
      chatPane.appendChild(el("div", { className: "loading-box" }, "메시지 불러오는 중..."));
      queryParams.tail = true;
    } else if (mode === "head") {
      state.messages = [];
      clear(chatPane);
      chatPane.appendChild(el("div", { className: "loading-box" }, "첫 메시지 불러오는 중..."));
      queryParams.offset = 0;
    } else if (mode === "around" && state.jumpTs) {
      state.messages = [];
      clear(chatPane);
      chatPane.appendChild(el("div", { className: "loading-box" }, "해당 시점 메시지 불러오는 중..."));
      queryParams.around_ts = state.jumpTs;
    } else if (mode === "older" && state.messages.length > 0) {
      queryParams.before_seq = state.messages[0].seq;
    } else if (mode === "newer" && state.messages.length > 0) {
      queryParams.offset = state.messages[state.messages.length - 1].seq + 1;
    }

    const res = await api.threadMessages(threadId, queryParams);
    state.total = res.total;
    if (mode === "older") {
      state.messages = [...res.items, ...state.messages];
      state.hasOlder = Boolean(res.has_older);
    } else if (mode === "newer") {
      state.messages = [...state.messages, ...res.items];
      state.hasNewer = Boolean(res.has_newer);
    } else {
      state.messages = [...res.items];
      state.hasOlder = Boolean(res.has_older);
      state.hasNewer = Boolean(res.has_newer);
    }

    clear(chatPane);
    const t = res.thread;
    const participantNodes = (t.participants || []).map((p) =>
      personButton(p.username, p.full_name, ctx.openPerson)
    );

    const header = el(
      "div",
      { className: "chat-header" },
      el(
        "div",
        { style: "display:flex;gap:10px;align-items:center;" },
        t.avatar_local_path ? avatarEl(t.avatar_local_path, t.title || t.thread_id) : null,
        el(
          "div",
          null,
          el(
            "div",
            { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;" },
            el("strong", null, t.title || t.thread_id),
            el("span", { className: "pill pill-accent" }, t.platform.toUpperCase()),
            t.is_e2ee ? el("span", { className: "pill pill-warn" }, "종단간 암호화 (E2EE)") : null
          ),
          participantNodes.length > 0
            ? el("div", { className: "post-meta-row", style: "margin-top:4px;" }, participantNodes)
            : null
        )
      ),
      el(
        "div",
        { style: "display:flex;gap:6px;align-items:center;flex-wrap:wrap;" },
        state.total > state.limit
          ? el(
              "button",
              { type: "button", className: "btn", onClick: () => loadThreadMessages(threadId, "head") },
              "처음부터"
            )
          : null,
        state.total > state.limit
          ? el(
              "button",
              { type: "button", className: "btn", onClick: () => loadThreadMessages(threadId, "tail") },
              "최신으로"
            )
          : null,
        el("span", { className: "pill" }, `${state.messages.length} / ${state.total} 메시지`)
      )
    );

    const scrollArea = el("div", { className: "chat-scroll" });
    if (state.hasOlder) {
      scrollArea.appendChild(
        el(
          "div",
          { className: "load-more-wrap" },
          el(
            "button",
            {
              type: "button",
              className: "btn",
              onClick: () => loadThreadMessages(threadId, "older"),
            },
            "⬆ 이전 메시지 더 불러오기"
          )
        )
      );
    }

    if (state.messages.length === 0) {
      scrollArea.appendChild(el("div", { className: "empty-box" }, "메시지가 없습니다."));
    } else {
      for (const m of state.messages) {
        const mediaGrid =
          m.media && m.media.length > 0
            ? el(
                "div",
                { className: "msg-media-grid" },
                m.media.map((med) => {
                  const src = mediaUrl(med.local_path);
                  if (!src) return el("span", { className: "pill" }, med.original_uri || "미디어 없음");
                  if (isAudioMedia(med)) {
                    return el("audio", { src, controls: true, preload: "metadata" });
                  }
                  if (isVideoMedia(med)) {
                    return el("video", { src, controls: true, playsinline: true, preload: "metadata" });
                  }
                  if (isDocumentMedia(med)) {
                    const fname = (med.local_path || med.original_uri || "첨부파일").split("/").pop();
                    return el(
                      "a",
                      {
                        className: "pill",
                        href: src,
                        target: "_blank",
                        rel: "noopener noreferrer",
                      },
                      `📎 ${fname}`
                    );
                  }
                  return el("img", {
                    src,
                    alt: "message attachment",
                    loading: "lazy",
                    style: "cursor:zoom-in;",
                    onClick: () => ctx.openLightbox([med], 0),
                  });
                })
              )
            : null;

        const reactionsRow =
          m.reactions && m.reactions.length > 0
            ? el(
                "div",
                { className: "post-meta-row" },
                m.reactions.map((r) => el("span", { className: "pill" }, `${r.reaction || r.emoji || ""} ${r.actor}`.trim()))
              )
            : null;

        const shareHref = safeExternalHref(m.shared_link);
        const shareBlock =
          m.shared_link || m.share_text || m.share_owner
            ? el(
                "div",
                { className: "shared-box", style: "word-break:break-all;margin-top:6px;" },
                m.share_owner ? el("div", { style: "font-weight:600;font-size:0.82rem;" }, `@${m.share_owner}`) : null,
                m.share_text ? el("div", { style: "margin:4px 0;white-space:pre-wrap;" }, m.share_text) : null,
                m.shared_link
                  ? shareHref
                    ? el("a", { href: shareHref, target: "_blank", rel: "noopener noreferrer" }, m.shared_link)
                    : el("div", null, m.shared_link)
                  : null
              )
            : null;

        const isHighlighted = state.jumpTs && m.timestamp_unix && Math.abs(m.timestamp_unix - state.jumpTs) <= 2;

        scrollArea.appendChild(
          el(
            "div",
            {
              className: "msg-bubble",
              style: isHighlighted ? "outline:2px solid var(--accent);" : undefined,
            },
            el(
              "div",
              { className: "msg-head" },
              personButton(null, m.sender || "알 수 없음", ctx.openPerson),
              el("span", null, formatDate(m.created_at, m.timestamp_unix))
            ),
            m.is_unsent
              ? el("div", { style: "font-style:italic;color:var(--text-muted);" }, "(전송 취소된 메시지)")
              : m.text
                ? el("div", { className: "msg-text" }, m.text)
                : null,
            m.is_missed_call
              ? el("div", { className: "pill pill-warn", style: "margin-top:6px;" }, "📵 부재중 통화")
              : null,
            m.call_duration !== null && m.call_duration !== undefined && m.call_duration > 0
              ? el("div", { className: "pill", style: "margin-top:6px;" }, `통화 시간 ${formatDuration(m.call_duration)}`)
              : null,
            shareBlock,
            mediaGrid,
            reactionsRow
          )
        );
      }
    }

    if (state.hasNewer) {
      scrollArea.appendChild(
        el(
          "div",
          { className: "load-more-wrap" },
          el(
            "button",
            {
              type: "button",
              className: "btn",
              onClick: () => loadThreadMessages(threadId, "newer"),
            },
            "⬇ 다음 메시지 더 불러오기"
          )
        )
      );
    }

    chatPane.append(header, scrollArea);
    if (mode === "tail") {
      requestAnimationFrame(() => {
        scrollArea.scrollTop = scrollArea.scrollHeight;
      });
    }
  }

  let searchTimer = null;
  platformSelect.addEventListener("change", () => {
    state.platform = platformSelect.value;
    state.selectedThreadId = null;
    loadThreads();
  });
  searchInput.addEventListener("input", () => {
    state.q = searchInput.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.selectedThreadId = null;
      loadThreads();
    }, 200);
  });

  await loadThreads();
}
