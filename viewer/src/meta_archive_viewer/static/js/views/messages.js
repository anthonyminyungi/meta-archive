/** Authentic Messenger / IG DM inbox and chat viewer with left/right bubbles, avatars, in-thread search, and media gallery. */

import { api, mediaUrl } from "../api.js?v=3";
import {
  avatarEl,
  clear,
  el,
  formatDate,
  formatDayKey,
  formatDayLabel,
  formatDuration,
  formatShortDate,
  formatTimeOnly,
  highlightTextNodes,
  isAudioMedia,
  isDocumentMedia,
  isVideoMedia,
  personButton,
  safeExternalHref,
} from "../dom.js?v=3";
import { icon } from "../icons.js?v=3";

export async function renderMessagesView(container, ctx, initialThreadId = null, initialTs = null) {
  clear(container);

  const state = {
    platform: "",
    sort: "recent",
    q: "",
    selectedThreadId: initialThreadId,
    jumpTs: initialTs ? Number(initialTs) : null,
    limit: 100,
    total: 0,
    hasOlder: false,
    hasNewer: false,
    messages: [],
    activeSubpanel: null, // null | "search" | "media"
    inThreadQ: "",
  };

  const platformSelect = el(
    "select",
    { className: "select", style: "flex:1;", "aria-label": "메시지 플랫폼" },
    el("option", { value: "" }, "전체 (IG + FB)"),
    el("option", { value: "instagram" }, "Instagram DM"),
    el("option", { value: "facebook" }, "FB Messenger")
  );
  const sortSelect = el(
    "select",
    { className: "select", "aria-label": "정렬 기준" },
    el("option", { value: "recent" }, "최신순"),
    el("option", { value: "messages" }, "메시지 많은순"),
    el("option", { value: "oldest" }, "오래된순")
  );
  const searchInput = el("input", {
    type: "search",
    className: "input",
    placeholder: "대화방 제목 / 참여자 검색...",
  });
  const threadsCountEl = el("div", { className: "tabular", style: "font-size:0.75rem;color:var(--text-muted);padding:0 2px;" }, "");
  const threadsListEl = el("div", { className: "thread-list" });
  const chatPane = el(
    "section",
    { className: "card chat-pane" },
    el("div", { className: "empty-box", style: "margin:auto;border:none;background:transparent;" }, "좌측에서 대화방을 선택하세요.")
  );

  const shell = el(
    "div",
    { className: "messages-shell" },
    el(
      "aside",
      { className: "card threads-pane" },
      el(
        "div",
        { className: "threads-controls" },
        el("div", { className: "threads-filter-row" }, platformSelect, sortSelect),
        searchInput,
        threadsCountEl
      ),
      threadsListEl
    ),
    chatPane
  );
  container.appendChild(shell);

  async function loadThreads() {
    clear(threadsListEl);
    threadsListEl.appendChild(el("div", { className: "loading-box", style: "border:none;" }, "대화 목록 불러오는 중..."));
    const res = await api.threads({
      platform: state.platform || undefined,
      sort: state.sort,
      q: state.q || undefined,
      limit: 300,
    });
    threadsCountEl.textContent = `대화방 ${res.total.toLocaleString()}개`;
    clear(threadsListEl);
    if (res.items.length === 0) {
      threadsListEl.appendChild(el("div", { className: "empty-box", style: "border:none;" }, "저장된 대화방이 없습니다."));
      return;
    }

    if (!state.selectedThreadId) {
      state.selectedThreadId = res.items[0].thread_id;
    }

    for (const t of res.items) {
      const platCode = t.platform === "instagram" ? "ig" : "fb";
      const avatarWrap = el(
        "div",
        { className: "thread-avatar-wrap" },
        avatarEl(t.avatar_local_path, t.title || t.thread_id, "md"),
        el("span", { className: `thread-plat-dot ${platCode}` }, platCode.toUpperCase())
      );

      const btn = el(
        "button",
        {
          type: "button",
          className: `thread-item${t.thread_id === state.selectedThreadId ? " active" : ""}`,
          onClick: () => {
            state.selectedThreadId = t.thread_id;
            state.jumpTs = null;
            state.activeSubpanel = null;
            state.inThreadQ = "";
            loadThreadsHighlight();
            loadThreadMessages(t.thread_id, "tail");
          },
          dataset: { threadId: t.thread_id },
        },
        avatarWrap,
        el(
          "div",
          { className: "thread-item-body" },
          el(
            "div",
            { className: "thread-item-top" },
            el("span", { className: "thread-item-title" }, t.title || t.thread_id),
            t.last_timestamp_unix
              ? el("span", { className: "thread-item-time tabular" }, formatShortDate(null, t.last_timestamp_unix))
              : null
          ),
          t.last_message_preview
            ? el("div", { className: "thread-item-preview" }, t.last_message_preview)
            : null,
          el(
            "div",
            { className: "thread-item-meta tabular" },
            el("span", null, `${(t.message_count || 0).toLocaleString()}건`),
            t.is_e2ee ? el("span", { className: "meta-chip" }, "E2EE") : null,
            t.is_pending ? el("span", { className: "meta-chip" }, "요청함") : null
          )
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

  function renderSubpanel(threadId, hostEl) {
    clear(hostEl);
    if (!state.activeSubpanel) {
      hostEl.classList.add("hidden");
      return;
    }
    hostEl.classList.remove("hidden");

    if (state.activeSubpanel === "search") {
      const qInput = el("input", {
        type: "search",
        className: "input",
        value: state.inThreadQ,
        placeholder: "이 대화방 내 메시지 키워드 검색...",
      });
      const hitsEl = el("div", { style: "display:flex;flex-direction:column;gap:6px;" });
      hostEl.append(
        el(
          "div",
          { style: "display:flex;gap:8px;align-items:center;" },
          qInput,
          el(
            "button",
            {
              type: "button",
              className: "icon-btn",
              "aria-label": "검색 닫기",
              onClick: () => {
                state.activeSubpanel = null;
                renderSubpanel(threadId, hostEl);
              },
            },
            icon("close", 15)
          )
        ),
        hitsEl
      );

      async function doInThreadSearch() {
        const q = qInput.value.trim();
        state.inThreadQ = q;
        clear(hitsEl);
        if (!q) {
          hitsEl.appendChild(el("div", { style: "font-size:0.82rem;color:var(--text-muted);" }, "검색어를 입력하면 해당 시점으로 바로 이동할 수 있습니다."));
          return;
        }
        hitsEl.appendChild(el("div", { style: "font-size:0.82rem;color:var(--text-muted);" }, "검색 중..."));
        const res = await api.threadMessages(threadId, { q, limit: 60 });
        clear(hitsEl);
        if (res.items.length === 0) {
          hitsEl.appendChild(el("div", { style: "font-size:0.82rem;color:var(--text-muted);" }, `'${q}' 일치 결과 없음`));
          return;
        }
        hitsEl.appendChild(el("div", { className: "tabular", style: "font-size:0.78rem;color:var(--text-muted);" }, `일치 ${res.total.toLocaleString()}건 (최근 ${res.items.length}건 표시)`));
        for (const hit of res.items) {
          hitsEl.appendChild(
            el(
              "button",
              {
                type: "button",
                className: "chat-search-hit",
                onClick: () => {
                  state.jumpTs = hit.timestamp_unix;
                  state.activeSubpanel = null;
                  loadThreadMessages(threadId, "around");
                },
              },
              el(
                "div",
                { className: "tabular", style: "display:flex;justify-content:space-between;gap:8px;font-size:0.75rem;color:var(--text-muted);" },
                el("strong", { style: "color:var(--text);" }, hit.sender || "알 수 없음"),
                el("span", null, formatDate(hit.created_at, hit.timestamp_unix))
              ),
              el("div", null, highlightTextNodes(hit.text || "(미디어)", q))
            )
          );
        }
      }

      let tmr = null;
      qInput.addEventListener("input", () => {
        clearTimeout(tmr);
        tmr = setTimeout(doInThreadSearch, 200);
      });
      requestAnimationFrame(() => qInput.focus());
      doInThreadSearch();
      return;
    }

    if (state.activeSubpanel === "media") {
      const headerRow = el(
        "div",
        { style: "display:flex;justify-content:space-between;align-items:center;" },
        el("strong", { style: "font-size:0.86rem;" }, "이 대화방의 사진·영상·첨부파일"),
        el(
          "button",
          {
            type: "button",
            className: "icon-btn",
            "aria-label": "미디어 닫기",
            onClick: () => {
              state.activeSubpanel = null;
              renderSubpanel(threadId, hostEl);
            },
          },
          icon("close", 15)
        )
      );
      const galHost = el("div", { style: "font-size:0.82rem;color:var(--text-muted);" }, "불러오는 중...");
      hostEl.append(headerRow, galHost);

      api.threadMessages(threadId, { media_only: true, limit: 150 }).then((res) => {
        clear(galHost);
        const allMedia = [];
        for (const m of res.items) {
          for (const med of m.media || []) {
            if (med && med.local_path) {
              allMedia.push({ ...med, _msg_ts: m.timestamp_unix });
            }
          }
        }
        if (allMedia.length === 0) {
          galHost.textContent = "이 대화방에 저장된 미디어가 없습니다.";
          return;
        }
        const grid = el("div", { className: "chat-media-gallery" });
        const lightboxItems = allMedia.filter((m) => !isAudioMedia(m) && !isDocumentMedia(m));
        for (const med of allMedia) {
          const src = mediaUrl(med.local_path);
          if (!src) continue;
          if (isAudioMedia(med)) {
            grid.appendChild(el("audio", { src, controls: true, preload: "metadata", style: "grid-column:span 2;width:100%;" }));
          } else if (isDocumentMedia(med)) {
            const fname = (med.local_path || "파일").split("/").pop();
            grid.appendChild(
              el("a", { className: "meta-chip", href: src, target: "_blank", rel: "noopener noreferrer" }, icon("file", 12), fname)
            );
          } else if (isVideoMedia(med)) {
            const idx = lightboxItems.indexOf(med);
            grid.appendChild(
              el("video", {
                className: "chat-media-thumb",
                src,
                muted: true,
                preload: "metadata",
                onClick: () => ctx.openLightbox(lightboxItems, Math.max(0, idx)),
              })
            );
          } else {
            const idx = lightboxItems.indexOf(med);
            grid.appendChild(
              el("img", {
                className: "chat-media-thumb",
                src,
                alt: "shared photo",
                loading: "lazy",
                decoding: "async",
                onClick: () => ctx.openLightbox(lightboxItems, Math.max(0, idx)),
              })
            );
          }
        }
        galHost.append(
          el("div", { className: "tabular", style: "font-size:0.76rem;color:var(--text-muted);margin-bottom:6px;" }, `최근 첨부 메시지 ${res.items.length} / ${res.total}건 (${allMedia.length}개 파일)`),
          grid
        );
      });
    }
  }

  async function loadThreadMessages(threadId, mode = "tail") {
    const prevScroll = chatPane.querySelector(".chat-scroll");
    const prevScrollHeight = prevScroll ? prevScroll.scrollHeight : 0;

    let queryParams = { limit: state.limit };
    if (mode === "tail") {
      state.messages = [];
      queryParams.tail = true;
    } else if (mode === "head") {
      state.messages = [];
      queryParams.offset = 0;
    } else if (mode === "around" && state.jumpTs) {
      state.messages = [];
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
    const platCode = t.platform === "instagram" ? "ig" : "fb";
    const ownerSet = new Set(
      (t.owner_names || []).map((n) => String(n).normalize("NFC").trim().toLowerCase())
    );
    const isGroupChat = (t.participants || []).length > 2;

    const participantNodes = (t.participants || []).map((p) =>
      personButton(p.username, p.full_name, ctx.openPerson)
    );

    const subbarEl = el("div", { className: "chat-subbar hidden" });
    const searchToggleBtn = el(
      "button",
      {
        type: "button",
        className: `btn btn-sm${state.activeSubpanel === "search" ? " btn-primary" : " btn-ghost"}`,
        onClick: () => {
          state.activeSubpanel = state.activeSubpanel === "search" ? null : "search";
          searchToggleBtn.className = `btn btn-sm${state.activeSubpanel === "search" ? " btn-primary" : " btn-ghost"}`;
          mediaToggleBtn.className = `btn btn-sm${state.activeSubpanel === "media" ? " btn-primary" : " btn-ghost"}`;
          renderSubpanel(threadId, subbarEl);
        },
      },
      icon("search", 14),
      "검색"
    );
    const mediaToggleBtn = el(
      "button",
      {
        type: "button",
        className: `btn btn-sm${state.activeSubpanel === "media" ? " btn-primary" : " btn-ghost"}`,
        onClick: () => {
          state.activeSubpanel = state.activeSubpanel === "media" ? null : "media";
          searchToggleBtn.className = `btn btn-sm${state.activeSubpanel === "search" ? " btn-primary" : " btn-ghost"}`;
          mediaToggleBtn.className = `btn btn-sm${state.activeSubpanel === "media" ? " btn-primary" : " btn-ghost"}`;
          renderSubpanel(threadId, subbarEl);
        },
      },
      icon("image", 14),
      "미디어"
    );

    const header = el(
      "div",
      { className: "chat-header" },
      el(
        "div",
        { style: "display:flex;gap:11px;align-items:center;min-width:0;" },
        avatarEl(t.avatar_local_path, t.title || t.thread_id, "md"),
        el(
          "div",
          { style: "min-width:0;" },
          el(
            "div",
            { style: "display:flex;gap:7px;align-items:center;flex-wrap:wrap;" },
            el("strong", { style: "font-size:0.98rem;letter-spacing:-0.015em;" }, t.title || t.thread_id),
            el("span", { className: `plat-tag ${t.platform}` }, platCode === "ig" ? "IG DM" : "Messenger"),
            t.is_e2ee ? el("span", { className: "meta-chip" }, "E2EE") : null
          ),
          participantNodes.length > 0
            ? el("div", { className: "post-meta-row", style: "margin-top:2px;font-size:0.78rem;" }, participantNodes)
            : null
        )
      ),
      el(
        "div",
        { style: "display:flex;gap:4px;align-items:center;flex-wrap:wrap;" },
        searchToggleBtn,
        mediaToggleBtn,
        state.total > state.limit
          ? el("button", { type: "button", className: "btn btn-sm btn-ghost", onClick: () => loadThreadMessages(threadId, "head") }, "처음")
          : null,
        state.total > state.limit
          ? el("button", { type: "button", className: "btn btn-sm btn-ghost", onClick: () => loadThreadMessages(threadId, "tail") }, "최신")
          : null,
        el("span", { className: "toolbar-count tabular", style: "margin-left:4px;" }, `${state.messages.length.toLocaleString()} / ${state.total.toLocaleString()}`)
      )
    );

    const scrollArea = el("div", { className: "chat-scroll" });
    if (state.hasOlder) {
      scrollArea.appendChild(
        el(
          "div",
          { className: "load-more-wrap", style: "margin:0 0 8px;" },
          el(
            "button",
            {
              type: "button",
              className: "btn btn-sm",
              onClick: () => loadThreadMessages(threadId, "older"),
            },
            icon("chevronUp", 14),
            "이전 메시지 더 불러오기"
          )
        )
      );
    }

    let highlightedNode = null;

    if (state.messages.length === 0) {
      scrollArea.appendChild(el("div", { className: "empty-box", style: "border:none;background:transparent;" }, "메시지가 없습니다."));
    } else {
      let lastDayKey = "";

      for (let i = 0; i < state.messages.length; i += 1) {
        const m = state.messages[i];
        const prevM = i > 0 ? state.messages[i - 1] : null;
        const nextM = i + 1 < state.messages.length ? state.messages[i + 1] : null;

        const dayKey = formatDayKey(m.created_at, m.timestamp_unix);
        if (dayKey && dayKey !== lastDayKey) {
          lastDayKey = dayKey;
          scrollArea.appendChild(
            el("div", { className: "msg-day-divider tabular" }, el("span", null, formatDayLabel(m.created_at, m.timestamp_unix)))
          );
        }

        const senderStr = (m.sender || "").normalize("NFC").trim();
        const senderLower = senderStr.toLowerCase();
        const isMine = Boolean(senderLower && ownerSet.has(senderLower));

        const prevSameSender =
          prevM &&
          formatDayKey(prevM.created_at, prevM.timestamp_unix) === dayKey &&
          (prevM.sender || "").normalize("NFC").trim().toLowerCase() === senderLower &&
          Math.abs((m.timestamp_unix || 0) - (prevM.timestamp_unix || 0)) <= 300;

        const nextSameSender =
          nextM &&
          formatDayKey(nextM.created_at, nextM.timestamp_unix) === dayKey &&
          (nextM.sender || "").normalize("NFC").trim().toLowerCase() === senderLower &&
          Math.abs((nextM.timestamp_unix || 0) - (m.timestamp_unix || 0)) <= 300;

        const mediaGrid =
          m.media && m.media.length > 0
            ? el(
                "div",
                { className: "msg-media-grid" },
                m.media.map((med) => {
                  const src = mediaUrl(med.local_path);
                  if (!src) return el("span", { className: "meta-chip" }, med.original_uri || "미디어 없음");
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
                      { className: "meta-chip", href: src, target: "_blank", rel: "noopener noreferrer" },
                      icon("file", 12),
                      fname
                    );
                  }
                  return el("img", {
                    src,
                    alt: "message attachment",
                    loading: "lazy",
                    decoding: "async",
                    style: "cursor:zoom-in;",
                    onClick: () => ctx.openLightbox([med], 0),
                  });
                })
              )
            : null;

        const shareHref = safeExternalHref(m.shared_link);
        const shareBlock =
          m.shared_link || m.share_text || m.share_owner
            ? el(
                "div",
                { className: "shared-box", style: "word-break:break-all;margin:6px 0 0;" },
                m.share_owner ? el("div", { style: "font-weight:600;font-size:0.8rem;" }, `@${m.share_owner}`) : null,
                m.share_text ? el("div", { style: "margin:3px 0;white-space:pre-wrap;" }, m.share_text) : null,
                m.shared_link
                  ? shareHref
                    ? el("a", { href: shareHref, target: "_blank", rel: "noopener noreferrer" }, m.shared_link)
                    : el("div", null, m.shared_link)
                  : null
              )
            : null;

        const reactionsRow =
          m.reactions && m.reactions.length > 0
            ? el(
                "div",
                { className: "msg-reactions-row" },
                m.reactions.map((r) =>
                  el(
                    "span",
                    { className: "msg-reaction-chip", title: r.actor || "" },
                    `${r.reaction || r.emoji || "❤️"} ${r.actor || ""}`.trim()
                  )
                )
              )
            : null;

        const isHighlighted =
          state.jumpTs && m.timestamp_unix && Math.abs(m.timestamp_unix - state.jumpTs) <= 2;

        const bubbleClasses = [
          "msg-bubble",
          isMine ? `msg-bubble-mine ${platCode}` : "msg-bubble-other",
          isHighlighted ? "msg-bubble-highlighted" : "",
        ]
          .filter(Boolean)
          .join(" ");

        const bubbleEl = el(
          "div",
          { className: bubbleClasses, title: formatDate(m.created_at, m.timestamp_unix) },
          m.is_unsent
            ? el("div", { className: "msg-unsent" }, "(전송 취소된 메시지)")
            : m.text
              ? el("div", { className: "msg-text" }, m.text)
              : null,
          m.is_missed_call
            ? el("div", { className: "inline-meta", style: "margin-top:4px;font-size:0.82rem;opacity:0.85;" }, icon("phoneMissed", 13), "부재중 통화")
            : null,
          m.call_duration !== null && m.call_duration !== undefined && m.call_duration > 0
            ? el("div", { className: "inline-meta tabular", style: "margin-top:4px;font-size:0.82rem;opacity:0.85;" }, icon("phone", 13), `통화 ${formatDuration(m.call_duration)}`)
            : null,
          shareBlock,
          mediaGrid
        );

        const timeLabel = !nextSameSender
          ? el("span", { className: "msg-time-side tabular" }, formatTimeOnly(m.created_at, m.timestamp_unix))
          : null;

        const avatarSlot = !isMine
          ? el(
              "div",
              { className: "msg-avatar-slot" },
              !nextSameSender
                ? el(
                    "button",
                    {
                      type: "button",
                      style: "border:none;background:transparent;padding:0;cursor:pointer;",
                      title: senderStr || "알 수 없음",
                      onClick: () => senderStr && ctx.openPerson(senderStr),
                    },
                    avatarEl(m.sender_avatar_local_path || (!isGroupChat ? t.avatar_local_path : null), senderStr || t.title || "?", "sm")
                  )
                : null
            )
          : null;

        const senderLabel =
          !isMine && !prevSameSender && senderStr
            ? el(
                "div",
                { className: "msg-sender-name" },
                personButton(null, senderStr, ctx.openPerson)
              )
            : null;

        const rowNode = el(
          "div",
          {
            className: `msg-row ${isMine ? "msg-row-mine" : "msg-row-other"}${prevSameSender ? " msg-row-grouped" : ""}`,
          },
          avatarSlot,
          el(
            "div",
            { className: "msg-col" },
            senderLabel,
            el("div", { className: "msg-bubble-line" }, bubbleEl, timeLabel),
            reactionsRow
          )
        );
        if (isHighlighted && !highlightedNode) {
          highlightedNode = rowNode;
        }
        scrollArea.appendChild(rowNode);
      }
    }

    if (state.hasNewer) {
      scrollArea.appendChild(
        el(
          "div",
          { className: "load-more-wrap", style: "margin:8px 0 0;" },
          el(
            "button",
            {
              type: "button",
              className: "btn btn-sm",
              onClick: () => loadThreadMessages(threadId, "newer"),
            },
            icon("chevronDown", 14),
            "다음 메시지 더 불러오기"
          )
        )
      );
    }

    chatPane.append(header, subbarEl, scrollArea);
    if (state.activeSubpanel) {
      renderSubpanel(threadId, subbarEl);
    }

    requestAnimationFrame(() => {
      if (mode === "tail") {
        scrollArea.scrollTop = scrollArea.scrollHeight;
      } else if (mode === "older" && prevScrollHeight > 0) {
        scrollArea.scrollTop = scrollArea.scrollHeight - prevScrollHeight;
      } else if (mode === "around" && highlightedNode) {
        highlightedNode.scrollIntoView({ block: "center" });
      }
    });
  }

  let searchTimer = null;
  platformSelect.addEventListener("change", () => {
    state.platform = platformSelect.value;
    state.selectedThreadId = null;
    loadThreads();
  });
  sortSelect.addEventListener("change", () => {
    state.sort = sortSelect.value;
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
