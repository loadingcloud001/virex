# SPDX-License-Identifier: Apache-2.0
// Alpine.js helpers used across the Virex portal.
//
// Loaded once via <script src="/static/js/alpine-helpers.js" defer></script>
// in base.html. Exports global functions referenced by `x-data="themeToggle(...)"`
// and `x-text="relativeTime(...)"`.

(function () {
  "use strict";

  // ---------------------------------------------------------------------
  // themeToggle(initialTheme)
  //   - initialTheme: "corporate" | "business" (from server-side render)
  //   - Reads/writes virex_theme cookie + localStorage so toggle is sticky.
  //   - Updates <html data-theme> on toggle.
  // ---------------------------------------------------------------------
  function themeToggle(initialTheme) {
    return {
      dark: initialTheme === "business",
      init() {
        // Sync from localStorage on subsequent loads so the toggle "sticks".
        try {
          const stored = localStorage.getItem("virex_theme");
          if (stored && stored !== initialTheme) {
            this.dark = stored === "business";
            this._applyToDom();
          }
        } catch (e) { /* localStorage unavailable; ignore */ }
      },
      toggle() {
        this.dark = !this.dark;
        this._applyToDom();
        this._persist();
      },
      _applyToDom() {
        document.documentElement.setAttribute(
          "data-theme", this.dark ? "business" : "corporate");
      },
      _persist() {
        try {
          localStorage.setItem(
            "virex_theme", this.dark ? "business" : "corporate");
        } catch (e) { /* ignore */ }
        document.cookie =
          "virex_theme=" + (this.dark ? "business" : "corporate")
          + "; path=/; max-age=31536000; SameSite=Lax";
      },
    };
  }

  // ---------------------------------------------------------------------
  // relativeTime(iso)
  //   - iso: ISO 8601 string OR Date-compatible value
  //   - Returns "5 min ago" / "in 2 days" / ISO date for >=7d.
  // ---------------------------------------------------------------------
  function relativeTime(iso) {
    const then = new Date(iso).getTime();
    const now = Date.now();
    const diff = Math.floor((now - then) / 1000);
    if (Math.abs(diff) < 60) return diff < -10 ? "in a moment" : "just now";
    const mins = Math.floor(Math.abs(diff) / 60);
    if (Math.abs(mins) < 60) {
      return (diff < 0 ? "in " : "") + mins + " min" + (mins === 1 ? "" : "s")
        + (diff < 0 ? "" : " ago");
    }
    const hrs = Math.floor(Math.abs(mins) / 60);
    if (Math.abs(hrs) < 24) {
      return (diff < 0 ? "in " : "") + hrs + "h" + (diff < 0 ? "" : " ago");
    }
    const days = Math.floor(Math.abs(hrs) / 24);
    if (Math.abs(days) < 7) {
      return (diff < 0 ? "in " : "") + days + "d" + (diff < 0 ? "" : " ago");
    }
    return new Date(iso).toISOString().slice(0, 10);
  }

  // Register with Alpine when it boots.
  document.addEventListener("alpine:init", () => {
    window.themeToggle = themeToggle;
    window.relativeTime = relativeTime;
  });
})();