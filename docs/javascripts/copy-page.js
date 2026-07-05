/**
 * copy-page.js — AI-first "Copy as Markdown" + "View as Markdown" affordances.
 *
 * URL-mapping scheme
 * ------------------
 * MkDocs uses directory URLs: source ``install.md`` → URL ``/install/``.
 * We emit the raw Markdown alongside the HTML at the *source* path:
 *   URL /install/        → raw MD at /install.md     (relative to site root)
 *   URL /               → raw MD at /index.md
 *   URL /relay/install/ → raw MD at /relay/install.md (handles GH Pages subpath)
 *
 * The mapping logic:
 *   1. Take window.location.pathname
 *   2. Strip trailing "index.html" if present
 *   3. Strip trailing "/"
 *   4. If empty (site root), use "/index"
 *   5. Append ".md"
 *   6. Prepend window.location.origin to get an absolute URL for fetch()
 *
 * This file is injected on every page via extra_javascript in mkdocs.yml.
 */

(function () {
  "use strict";

  /** Derive the absolute URL of this page's raw .md file. */
  function mdUrl() {
    var pathname = window.location.pathname;
    // Strip trailing index.html
    pathname = pathname.replace(/\/index\.html$/, "/");
    // Strip trailing slash — but keep at least "/"
    if (pathname.length > 1) {
      pathname = pathname.replace(/\/$/, "");
    }
    // Root maps to /index
    if (pathname === "" || pathname === "/") {
      pathname = "/index";
    }
    return window.location.origin + pathname + ".md";
  }

  /** Build the toolbar element with the button and link. */
  function buildToolbar() {
    var bar = document.createElement("div");
    bar.className = "relay-ai-toolbar";

    // ── Copy as Markdown button ─────────────────────────────────────────────
    var btn = document.createElement("button");
    btn.className = "relay-ai-copy-btn";
    btn.setAttribute("aria-label", "Copy this page as Markdown for use in an AI prompt");
    btn.innerHTML =
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true">' +
      "<path d=\"M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z\"/>" +
      "</svg>" +
      " Copy as Markdown";

    btn.addEventListener("click", function () {
      var url = mdUrl();
      fetch(url)
        .then(function (resp) {
          if (!resp.ok) {
            throw new Error("HTTP " + resp.status);
          }
          return resp.text();
        })
        .then(function (text) {
          return navigator.clipboard.writeText(text);
        })
        .then(function () {
          btn.textContent = "Copied!";
          btn.classList.add("relay-ai-copy-btn--success");
          setTimeout(function () {
            btn.innerHTML =
              '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true">' +
              "<path d=\"M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z\"/>" +
              "</svg>" +
              " Copy as Markdown";
            btn.classList.remove("relay-ai-copy-btn--success");
          }, 2000);
        })
        .catch(function (err) {
          btn.textContent = "Copy failed";
          console.error("[relay] copy-page: fetch failed:", err);
          setTimeout(function () {
            btn.innerHTML =
              '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true">' +
              "<path d=\"M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z\"/>" +
              "</svg>" +
              " Copy as Markdown";
          }, 2000);
        });
    });

    // ── View as Markdown link ───────────────────────────────────────────────
    var link = document.createElement("a");
    link.className = "relay-ai-view-link";
    link.href = mdUrl();
    link.target = "_blank";
    link.rel = "noopener";
    link.setAttribute("aria-label", "View raw Markdown source for this page");
    link.innerHTML =
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true">' +
      "<path d=\"M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zM6 20V4h7v5h5v11H6z\"/>" +
      "</svg>" +
      " View as Markdown";

    bar.appendChild(btn);
    bar.appendChild(link);
    return bar;
  }

  /** Insert the toolbar just before the first H1 in the article content. */
  function inject() {
    // Material renders article content inside .md-content__inner
    var article = document.querySelector(".md-content__inner");
    if (!article) return;

    // Don't double-inject
    if (article.querySelector(".relay-ai-toolbar")) return;

    // Find the first H1 heading (the page title)
    var h1 = article.querySelector("h1");
    var toolbar = buildToolbar();

    if (h1) {
      // Insert immediately after the H1
      if (h1.nextSibling) {
        article.insertBefore(toolbar, h1.nextSibling);
      } else {
        article.appendChild(toolbar);
      }
    } else {
      // No H1 — prepend to article
      article.insertBefore(toolbar, article.firstChild);
    }
  }

  // Run after DOM is ready.  Material uses instant navigation (pushState) so
  // we also re-inject on the document$ observable if available (MkDocs Material
  // exposes this as a global).
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", inject);
  } else {
    inject();
  }

  // Re-inject on Material's SPA navigation events
  if (typeof window.document$ !== "undefined") {
    window.document$.subscribe(inject);
  }
})();
