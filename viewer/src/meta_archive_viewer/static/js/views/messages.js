/** Unified IG DM + FB Messenger inbox and thread viewer (supports E2EE threads and attached media). */

import { api, mediaUrl } from "../api.js";
import { clear, el, formatDate, formatDuration, isAudioMedia, isVideoMedia, personButton } from "../dom.js";

export async function renderMessagesView(container, ctx, initialThreadId = null) {
  clear(container);

  const state = {
    platform: "",
    q: "",
    selectedThreadId: initialThreadId,
    offset: 0,
    limit: 100,
    total: 0,
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
            loadThreadsHighlight();
            loadThreadMessages(t.thread_id, true);
          },
          dataset: { threadId: t.thread_id },
        },
        el(
          "div",
          { style: "display:flex;justify-content:space-between;gap:6px;align-items:center;" },
          el("strong", { style: "font-size:0.93rem;" }, t.title || t.thread_id),
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
      await loadThreadMessages(state.selectedThreadId, true);
    }
  }

  function loadThreadsHighlight() {
    for (const node of threadsListEl.querySelectorAll(".thread-item")) {
      node.classList.toggle("active", node.dataset.threadId === state.selectedThreadId);
    }
  }

  async function loadThreadMessages(threadId, reset = true) {
    if (reset) {
      state.offset = 0;
      state.messages = [];
      clear(chatPane);
      chatPane.appendChild(el("div", { className: "loading-box" }, "메시지 불러오는 중..."));
    }

    const res = await api.threadMessages(threadId, { offset: state.offset, limit: state.limit });
    state.total = res.total;
    state.messages.push(...res.items);
    state.offset += res.items.length;

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
      ),
      el("span", { className: "pill" }, `${state.messages.length} / ${state.total} 메시지`)
    );

    const scrollArea = el("div", { className: "chat-scroll" });
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
                  if (isVideoMedia(med)) {
                    return el("video", { src, controls: true, playsinline: true, preload: "metadata" });
                  }
                  if (isAudioMedia(med)) {
                    return el("audio", { src, controls: true, preload: "metadata" });
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
                m.reactions.map((r) => el("span", { className: "pill" }, `${r.emoji} ${r.actor}`))
              )
            : null;

        scrollArea.appendChild(
          el(
            "div",
            { className: "msg-bubble" },
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
            m.call_duration !== null && m.call_duration !== undefined
              ? el("div", { className: "pill", style: "margin-top:6px;" }, `통화 시간 ${formatDuration(m.call_duration)}`)
              : null,
            m.shared_link
              ? el("div", { className: "shared-box", style: "word-break:break-all;" }, m.shared_link)
              : null,
            mediaGrid,
            reactionsRow
          )
        );
      }
    }

    if (state.messages.length < state.total) {
      scrollArea.appendChild(
        el(
          "div",
          { className: "load-more-wrap" },
          el(
            "button",
            {
              type: "button",
              className: "btn",
              onClick: () => loadThreadMessages(threadId, false),
            },
            `이전/다음 메시지 더 불러오기 (${state.messages.length}/${state.total})`
          )
        )
      );
    }

    chatPane.append(header, scrollArea);
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
