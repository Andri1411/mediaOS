// Spatial navigation for desktop web sites: the arrow keys move a focus ring
// between the things one can click, Enter activates. The controller's d-pad
// and A arrive here as those keys.
//
// Site-specific knowledge lives in sites/<site>.js, which runs first and may
// set window.tvnavSite = {
//   player()    -> true while the site's video player has the screen: keys
//                  are left to the site's own shortcuts
//   candidates  extra CSS selector for things to navigate to
//   ignore      CSS selector for things never to navigate to
// }
// A broken or outdated site file only affects that site; without one the
// generic rules below apply.
'use strict';

(() => {
  if (window.top !== window) return;          // frames are not navigated separately
  // The hub checks for this after a service starts (see apps.check_navigation).
  document.documentElement.dataset.tvnav = 'on';
  const site = window.tvnavSite ?? {};
  const GENERIC = 'a[href], button, input, select, textarea, summary, [role="button"], [role="link"], '
    + '[role="menuitem"], [role="tab"], [role="option"], [role="checkbox"], [tabindex]:not([tabindex="-1"])';
  const ARROWS = { ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] };
  let current = null;

  const isText = (el) => el instanceof HTMLTextAreaElement || el?.isContentEditable
    || (el instanceof HTMLInputElement && !/^(button|checkbox|radio|submit|reset|range|color|file|image)$/.test(el.type));

  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) return false;
    return !el.disabled && el.getAttribute('aria-hidden') !== 'true';
  }

  // Dialogs on top of the page (cookie banners on a first visit, profile
  // pickers, confirmations) keep the focus until they are dealt with.
  const DIALOGS = 'dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"], '
    + '#onetrust-banner-sdk, #onetrust-pc-sdk';

  function candidates() {
    const selector = site.candidates ? `${GENERIC}, ${site.candidates}` : GENERIC;
    const all = [...document.querySelectorAll(selector)].filter((el) =>
      visible(el) && !(site.ignore && el.closest(site.ignore)));
    // Boxes around other targets are not targets themselves (a cookie banner
    // with tabindex="0" around its buttons): the focus goes to what is inside.
    const targets = all.filter((el) => !all.some((other) => other !== el && el.contains(other)));
    const dialogs = [...document.querySelectorAll(DIALOGS)].filter((d) => visible(d) && targets.some((el) => d.contains(el)));
    const top = dialogs[dialogs.length - 1];
    dialogOpen = Boolean(top);
    return top ? targets.filter((el) => top.contains(el)) : targets;
  }
  let dialogOpen = false;

  function setFocus(el) {
    current?.removeAttribute('data-tvnav-focus');
    current = el;
    if (!el) return;
    el.setAttribute('data-tvnav-focus', '');
    el.focus({ preventScroll: true });
    el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'smooth' });
  }

  // The best candidate in direction (dx, dy). Elements in line with the
  // current one (overlapping it sideways) win, nearest first; only when there
  // is none does the nearest off-line element get the focus.
  function next(from, dx, dy) {
    const a = from.getBoundingClientRect();
    const ax = a.left + a.width / 2, ay = a.top + a.height / 2;
    let best = null, bestScore = Infinity;
    for (const el of candidates()) {
      if (el === from || el.contains(from) || from.contains(el)) continue;
      const b = el.getBoundingClientRect();
      const bx = b.left + b.width / 2, by = b.top + b.height / 2;
      const along = (bx - ax) * dx + (by - ay) * dy;
      const across = Math.abs((bx - ax) * dy) + Math.abs((by - ay) * dx);
      // must be clearly that way, not merely overlapping
      const edge = dx ? (dx > 0 ? b.left - a.right : a.left - b.right) : (dy > 0 ? b.top - a.bottom : a.top - b.bottom);
      if (along <= 0 || edge < -Math.min(a.width, a.height, b.width, b.height) / 2) continue;
      const inLine = dx ? (b.top < a.bottom && b.bottom > a.top) : (b.left < a.right && b.right > a.left);
      const score = inLine ? along : 1e6 + along + across * 2;
      if (score < bestScore) { best = el; bestScore = score; }
    }
    return best;
  }

  function first(within = null) {
    const all = candidates().filter((el) => {
      if (within && !within.contains(el)) return false;
      const r = el.getBoundingClientRect();
      return r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
    });
    // In a dialog the first button in reading order (not a link inside its
    // text, e.g. "cookie policy"); otherwise the top-left-most thing on screen.
    if (dialogOpen || within) {
      return all.find((el) => el.matches('button, [role="button"], input[type="submit"], input[type="button"]'))
        ?? all[0] ?? null;
    }
    return all.sort((p, q) => {
      const a = p.getBoundingClientRect(), b = q.getBoundingClientRect();
      return (a.top - b.top) || (a.left - b.left);
    })[0] ?? null;
  }

  addEventListener('keydown', (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    try {
      if (site.player?.()) return;
    } catch { /* outdated site file: fall back to generic behaviour */ }
    const arrow = ARROWS[event.key];
    const active = document.activeElement;
    if (arrow) {
      // In a text field left/right move the caret; up/down leave the field.
      if (isText(active) && arrow[0]) return;
      if (!current?.isConnected || !visible(current)) current = null;
      // The site may have focused a box around targets (e.g. a dialog): start inside it.
      const inside = current && candidates().some((el) => el !== current && current.contains(el));
      const target = !current ? first() : inside ? first(current) : next(current, arrow[0], arrow[1]);
      event.preventDefault();
      event.stopPropagation();
      if (target) setFocus(target);
      else if (current) scrollBy({ left: arrow[0] * innerWidth / 3, top: arrow[1] * innerHeight / 3, behavior: 'smooth' });
    } else if (event.key === 'Enter' && current?.isConnected && !isText(active)) {
      event.preventDefault();
      event.stopPropagation();
      current.click();
    }
  }, true);

  // Follow focus changes made by the site or the mouse.
  addEventListener('focusin', (event) => {
    if (event.target !== current && event.target instanceof Element && event.target !== document.body) {
      current?.removeAttribute('data-tvnav-focus');
      current = event.target;
      current.setAttribute('data-tvnav-focus', '');
    }
    if (isText(event.target)) chrome.runtime.sendMessage({ textFocus: true }).catch(() => {});
  }, true);
  addEventListener('focusout', (event) => {
    if (isText(event.target) && !isText(event.relatedTarget)) {
      chrome.runtime.sendMessage({ textFocus: false }).catch(() => {});
    }
  }, true);
})();
