// Shared helpers for the askweave.xyz account pages.
window.weave = {
  async api(path, data) {
    const opts = data === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json", "X-Weave": "1" }, body: JSON.stringify(data),
    };
    const res = await fetch(path, { credentials: "same-origin", ...opts });
    let body = {};
    try { body = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) {
      const err = new Error(typeof body.detail === "string" ? body.detail : "Something went wrong. Try again.");
      err.status = res.status;
      throw err;
    }
    return body;
  },
  el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c);
    return node;
  },
  $(sel) { return document.querySelector(sel); },
};
