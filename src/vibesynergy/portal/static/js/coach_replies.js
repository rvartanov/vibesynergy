// Replies to a coach report (Coach page and the report page a mail links to). A new reply
// is sent with its button; a reply the coach has not read yet can still be changed and
// saves as you type. The coach reads unread replies at its next run; "akkoord" or "ja" on a
// proposal approves it.
import { post } from "./api.js";
import { esc, userDateTime } from "./format.js";
import { t } from "./i18n.js";
import { bindAutosave } from "./settings_autosave.js";

export const REPLYABLE = new Set(["morning", "evening", "note"]);

function replyHtml(reply) {
  const id = esc(String(reply.id));
  const when = esc(userDateTime(reply.updated_at || reply.at));
  if (reply.read) {
    return `<li class="coach-reply"><p>${esc(reply.text)}</p>
      <p class="sub">${when} · ${esc(t("Read by your coach"))}</p></li>`;
  }
  return `<li class="coach-reply" data-reply-id="${id}">
    <label class="visually-hidden" for="reply-edit-${id}">${esc(t("Change your reply"))}</label>
    <textarea class="btn" id="reply-edit-${id}" rows="2" maxlength="4000" aria-describedby="reply-edit-${id}-hint">${esc(reply.text)}</textarea>
    <p class="sub" id="reply-edit-${id}-hint">${when} · ${esc(t("Not read yet. Changes save as you type."))}</p>
    <p class="sub" role="status" aria-live="polite" data-reply-edit-status></p>
  </li>`;
}

export function repliesHtml(entry) {
  if (!entry?.id || !REPLYABLE.has(entry.entry_type)) return "";
  const id = esc(String(entry.id));
  const replies = entry.replies || [];
  return `<div class="coach-replies" data-replies="${id}">
    ${replies.length ? `<ul class="coach-reply-list">${replies.map(replyHtml).join("")}</ul>` : ""}
    <label for="reply-new-${id}">${esc(t("Reply to your coach"))}</label>
    <textarea class="btn" id="reply-new-${id}" rows="2" maxlength="4000" aria-describedby="reply-new-${id}-hint"></textarea>
    <p class="sub" id="reply-new-${id}-hint">${esc(t("Your coach reads this at the next run. Write “akkoord” to approve a proposal."))}</p>
    <div class="settings-save"><button class="btn" type="button" data-reply-send>${esc(t("Send reply"))}</button></div>
    <p class="sub" role="status" aria-live="polite" data-reply-status></p>
  </div>`;
}

function bindEdits(block) {
  for (const item of block.querySelectorAll("[data-reply-id]")) {
    const area = item.querySelector("textarea");
    bindAutosave(item, {
      statusEl: item.querySelector("[data-reply-edit-status]"),
      save: async () => {
        await post(`coach/replies/${item.dataset.replyId}`, { text: area.value });
      },
    });
  }
}

// Wire every reply block under root. entryFor(id) returns the entry object to update.
export function bindReplies(root, entryFor) {
  for (const block of root.querySelectorAll("[data-replies]")) {
    bindEdits(block);
    const button = block.querySelector("[data-reply-send]");
    const area = block.querySelector(`#reply-new-${CSS.escape(block.dataset.replies)}`);
    const status = block.querySelector("[data-reply-status]");
    button.addEventListener("click", async () => {
      if (!area.value.trim()) {
        status.textContent = t("Write a reply first.");
        area.focus();
        return;
      }
      button.disabled = true;
      status.textContent = t("Saving…");
      try {
        const { data } = await post("coach/replies", { entry_id: Number(block.dataset.replies), text: area.value });
        const entry = entryFor(Number(block.dataset.replies));
        if (entry) entry.replies = [...(entry.replies || []), data];
        const fresh = document.createElement("div");
        fresh.innerHTML = repliesHtml(entry || { id: data.entry_id, entry_type: "note", replies: [data] });
        const next = fresh.firstElementChild;
        block.replaceWith(next);
        bindReplies(next.parentElement, entryFor);
        const nextStatus = next.querySelector("[data-reply-status]");
        nextStatus.textContent = t("Sent. Your coach reads it at the next run.");
        next.querySelector("textarea[id^='reply-new-']")?.focus();
      } catch (error) {
        status.textContent = t(error.message);
        button.disabled = false;
      }
    });
  }
}
