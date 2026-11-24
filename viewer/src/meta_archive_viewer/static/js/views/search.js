/** Hybrid Korean Search view with safe <mark> keyword highlighting and clean metadata chips. */

import { api, mediaUrl } from "../api.js?v=3";
import { clear, el, formatDate, highlightTextNodes, isVideoMedia, safeExternalHref, skeletonCards } from "../dom.js?v=3";
import { icon } from "../icons.js?v=3";

const TYPE_LABELS = {
  post: "게시물",
  comment: "댓글",
  message: "메시지",
  story: "스토리",
  connection: "인물",
  activity: "내 활동",
};

export async function renderSearchView(container, ctx, initialQuery = "") {
  clear(container);

  const queryInput = el("input", {
    type: "search",
    className: "input",
    value: initialQuery,
    placeholder: "검색어 입력 (단축키 '/' · 한글 1~2글자 및 3글자 이상 모두 지원)",
  });
  const platformSelect = el(
    "select",
    { className: "select", "aria-label": "플랫폼 필터" },
    el("option", { value: "" }, "전체 플랫폼"),
    el("option", { value: "instagram" }, "Instagram"),
    el("option", { value: "facebook" }, "Facebook")
  );
  const entitySelect = el(
    "select",
    { className: "select", "aria-label": "대상 필터" },
    el("option", { value: "" }, "전체 유형"),
    el("option", { value: "post" }, "게시물"),
    el("option", { value: "comment" }, "댓글"),
    el("option", { value: "message" }, "메시지"),
    el("option", { value: "story" }, "스토리"),
    el("option", { value: "activity" }, "내 활동"),
    el("option", { value: "connection" }, "인물")
  );
  const submitBtn = el("button", { type: "submit", className: "btn btn-primary" }, icon("search", 15), "검색");

  const statusRow = el("div", { className: "post-meta-row tabular", style: "margin-bottom:12px;" });
  const resultsEl = el("div", { className: "feed-list" });

  const form = el(
    "form",
    { className: "card", style: "margin-bottom:14px;" },
    el("div", { className: "search-bar", style: "margin-bottom:0;" }, queryInput, platformSelect, entitySelect, submitBtn)
  );

  container.append(form, statusRow, resultsEl);

  async function runSearch() {
    const q = queryInput.value.trim();
    clear(statusRow);
    clear(resultsEl);
    if (!q) {
      resultsEl.appendChild(
        el("div", { className: "empty-box" }, "검색어를 입력하면 게시물, 댓글, DM, 스토리, 인물 전체를 조회합니다.")
      );
      return;
    }

    resultsEl.appendChild(skeletonCards(3));
    const res = await api.search({
      q,
      platform: platformSelect.value || undefined,
      entity_type: entitySelect.value || undefined,
      limit: 100,
    });

    clear(statusRow);
    const modeLabel =
      res.mode === "fts5_trigram" ? "FTS5 Trigram" : "단문 검색";
    statusRow.append(
      el("span", { style: "font-weight:600;color:var(--text);" }, `검색 결과 ${res.total.toLocaleString()}건`),
      el("span", { className: "meta-chip" }, modeLabel)
    );

    clear(resultsEl);
    if (res.items.length === 0) {
      resultsEl.appendChild(el("div", { className: "empty-box" }, `'${res.query}'에 일치하는 항목이 없습니다.`));
      return;
    }

    for (const item of res.items) {
      const snippet = (item.body || item.entity_id || "").slice(0, 320);

      const card = el(
        "button",
        {
          type: "button",
          className: "card search-result-card",
          onClick: () => {
            if (item.entity_type === "post" || item.entity_type === "comment") {
              ctx.openPost(item.entity_id);
            } else if (item.entity_type === "message") {
              ctx.navigateThread(item.entity_id, item.timestamp_unix);
            } else if (item.entity_type === "story") {
              ctx.modal.openCustom(`스토리 — ${item.platform}`, async (body) => {
                const stRes = await api.stories({ platform: item.platform, story_id: item.entity_id, limit: 1 });
                clear(body);
                const s = stRes.items && stRes.items[0];
                if (!s) {
                  body.appendChild(el("div", { className: "empty-box" }, "스토리를 찾을 수 없습니다."));
                  return;
                }
                const src = s.media ? mediaUrl(s.media.local_path) : null;
                body.appendChild(
                  el(
                    "div",
                    { className: "card" },
                    el(
                      "div",
                      { className: "post-meta-row tabular", style: "margin-top:0;margin-bottom:8px;" },
                      el("span", { className: "meta-chip" }, formatDate(s.created_at, s.timestamp_unix)),
                      s.highlight_title ? el("span", { className: "meta-chip" }, s.highlight_title) : null
                    ),
                    src
                      ? isVideoMedia(s.media)
                        ? el("video", { src, controls: true, playsinline: true, style: "max-width:100%;max-height:460px;border-radius:8px;" })
                        : el("img", { src, alt: "story", decoding: "async", style: "max-width:100%;max-height:460px;border-radius:8px;cursor:zoom-in;", onClick: () => ctx.openLightbox([s.media], 0) })
                      : null,
                    s.caption ? el("div", { className: "post-caption" }, s.caption) : null
                  )
                );
              });
            } else if (item.entity_type === "activity") {
              ctx.modal.openCustom("내 활동 상세", async (body) => {
                const actRes = await api.activities({ activity_id: item.entity_id, limit: 1 });
                clear(body);
                const a = actRes.items && actRes.items[0];
                if (!a) {
                  body.appendChild(el("div", { className: "empty-box" }, "활동 내역을 찾을 수 없습니다."));
                  return;
                }
                const href = safeExternalHref(a.url);
                body.appendChild(
                  el(
                    "div",
                    { className: "card" },
                    el(
                      "div",
                      { className: "post-meta-row tabular", style: "margin-top:0;margin-bottom:8px;" },
                      el("span", { className: "meta-chip" }, a.kind),
                      el("span", null, formatDate(a.created_at, a.timestamp_unix)),
                      a.target_author ? el("span", { className: "meta-chip" }, a.target_author) : null,
                      a.group_name ? el("span", { className: "meta-chip" }, a.group_name) : null
                    ),
                    a.title ? el("div", { className: "activity-subtitle" }, a.title) : null,
                    a.text ? el("div", { className: "post-caption" }, a.text) : null,
                    href ? el("a", { href, target: "_blank", rel: "noopener noreferrer", style: "font-size:0.82rem;word-break:break-all;" }, a.url) : null
                  )
                );
              });
            } else if (item.entity_type === "connection") {
              ctx.openPerson(item.entity_id || item.author);
            }
          },
        },
        el(
          "div",
          { className: "post-meta-row tabular", style: "margin-top:0;margin-bottom:6px;" },
          el("span", { className: "meta-chip" }, TYPE_LABELS[item.entity_type] || item.entity_type),
          el("span", { className: `plat-tag ${item.platform}` }, item.platform === "instagram" ? "IG" : "FB"),
          item.author ? el("strong", { style: "color:var(--text);" }, highlightTextNodes(item.author, q)) : null,
          item.timestamp_unix ? el("span", null, formatDate(null, item.timestamp_unix)) : null
        ),
        el("div", { style: "white-space:pre-wrap;font-size:0.92rem;color:var(--text);" }, highlightTextNodes(snippet, q))
      );
      resultsEl.appendChild(card);
    }
  }

  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    runSearch();
  });
  platformSelect.addEventListener("change", runSearch);
  entitySelect.addEventListener("change", runSearch);

  requestAnimationFrame(() => queryInput.focus());
  await runSearch();
}
