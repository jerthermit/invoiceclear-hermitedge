/* Progressive interface feedback only. No requests, approvals or business state. */
(() => {
  "use strict";
  const motion = window.matchMedia?.("(prefers-reduced-motion: reduce)");
  const active = new Map();
  let initialized = false, noticeTimer, stageTarget;
  function cancel(element) {
    try { active.get(element)?.cancel(); } catch { /* Optional motion. */ }
    active.delete(element);
  }
  function animate(element, frames, options) {
    cancel(element);
    if (!element || motion?.matches || document.hidden || typeof element.animate !== "function") return;
    try {
      const animation = element.animate(frames, options);
      active.set(element, animation);
      const release = () => { if (active.get(element) === animation) active.delete(element); };
      animation.finished?.then(release, release);
    } catch { /* An unsupported effect must never block a task. */ }
  }
  function reveal(element, direction = 1) {
    animate(element, [{opacity:.65,transform:`translateX(${direction < 0 ? -10 : 10}px)`},{opacity:1,transform:"translateX(0)"}], {duration:210,easing:"cubic-bezier(.16,1,.3,1)"});
  }
  function stage(element) {
    if (stageTarget !== element) cancel(stageTarget);
    stageTarget = element;
    animate(element, [{opacity:.5,transform:"translateY(3px)"},{opacity:1,transform:"translateY(0)"}], {duration:180,easing:"cubic-bezier(.16,1,.3,1)"});
  }
  function spotlight(element) {
    animate(element, [{backgroundColor:"#cdb27b",boxShadow:"0 0 0 3px #cdb27b"},{backgroundColor:"#e8d8a6",boxShadow:"0 0 0 2px #e8d8a6"}], {duration:750,easing:"ease-out"});
  }
  function filed(sheet) {
    animate(sheet?.querySelector(".handoff-art"), [{opacity:.6,transform:"translateY(-6px)"},{opacity:1,transform:"translateY(0)"}], {duration:360,easing:"cubic-bezier(.16,1,.3,1)"});
  }
  function fileError(files) {
    if (files?.length !== 1) return "Choose one PDF or TXT document.";
    const file = files[0];
    if (!/\.(pdf|txt)$/i.test(file.name)) return "Choose a PDF or TXT document.";
    if (file.size > 5 * 1024 * 1024) return "This document exceeds 5 MB. Choose a smaller file.";
    if (!file.size) return "This document is empty. Choose a readable PDF or TXT.";
    return "";
  }
  function dismissNotice() {
    clearTimeout(noticeTimer);
    const notice = document.querySelector("#notice");
    if (notice) notice.hidden = true;
  }
  function scheduleNotice() {
    clearTimeout(noticeTimer);
    const notice = document.querySelector("#notice");
    if (!notice || notice.hidden || notice.classList.contains("error") || notice.matches(":hover, :focus-within")) return;
    noticeTimer = setTimeout(dismissNotice, 7000);
  }
  function showNotice(error = false) {
    clearTimeout(noticeTimer);
    const notice = document.querySelector("#notice");
    if (!notice) return;
    notice.hidden = false;
    animate(notice, [{opacity:0,transform:"translateY(6px)"},{opacity:1,transform:"translateY(0)"}], {duration:170,easing:"ease-out"});
    if (!error) scheduleNotice();
  }
  function init({onFind, onFiles} = {}) {
    if (initialized) return;
    initialized = true;
    document.addEventListener("keydown", event => {
      if (event.defaultPrevented || event.repeat || event.isComposing || event.altKey || event.shiftKey || !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "k") return;
      if (document.querySelector("#app-shell")?.hidden !== false || document.querySelector("dialog[open]")) return;
      event.preventDefault();
      Promise.resolve(onFind?.()).catch(() => {});
    });
    const targetForm = event => {
      const form = event.target.closest?.("#upload-form");
      return form && !form.closest("[hidden],dialog:not([open])") && form.getAttribute("aria-busy") !== "true" ? form : null;
    };
    const keepFileInWorkspace = event => {
      if (Array.from(event.dataTransfer?.types || []).includes("Files") && event.target.closest?.("#app-shell")) event.preventDefault();
    };
    for (const type of ["dragenter","dragover"]) document.addEventListener(type, event => {
      keepFileInWorkspace(event);
      const form = targetForm(event);
      if (!form || !Array.from(event.dataTransfer?.types || []).includes("Files")) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      form.classList.add("file-drag");
    });
    document.addEventListener("dragleave", event => {
      const form = targetForm(event);
      if (form && !form.contains(event.relatedTarget)) form.classList.remove("file-drag");
    });
    document.addEventListener("drop", event => {
      keepFileInWorkspace(event);
      const form = targetForm(event);
      if (!form) return;
      event.preventDefault();
      form.classList.remove("file-drag");
      onFiles?.(event.dataTransfer?.files || [], form, true);
    });
    document.addEventListener("change", event => {
      const form = targetForm(event);
      if (form && event.target.id === "document-file") onFiles?.(event.target.files || [], form, false);
    });
    const notice = document.querySelector("#notice");
    for (const type of ["mouseenter","focusin"]) notice?.addEventListener(type, () => clearTimeout(noticeTimer));
    for (const type of ["mouseleave","focusout"]) notice?.addEventListener(type, scheduleNotice);
    motion?.addEventListener?.("change", () => { if (motion.matches) active.forEach((_, element) => cancel(element)); });
    document.addEventListener("visibilitychange", () => { if (document.hidden) active.forEach((_, element) => cancel(element)); });
    const apple = /Mac|iPhone|iPad/.test(navigator.platform || "");
    const search = document.querySelector("#queue-search");
    search?.setAttribute("aria-keyshortcuts",apple ? "Meta+K" : "Control+K");
    const label = search?.closest(".queue-search");
    if (label) label.dataset.shortcut = apple ? "⌘ K" : "Ctrl K";
  }
  window.InvoiceClearUI = Object.freeze({reveal,stage,spotlight,filed,fileError,showNotice,dismissNotice,init});
})();
