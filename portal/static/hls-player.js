# SPDX-License-Identifier: Apache-2.0
// HLS player for the Virex camera detail page.
//
// Loaded only on /cameras/{id} pages via:
//   <script src="/static/hls-player.js" defer></script>
//   <script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js" defer></script>
//
// Exports an Alpine `hlsPlayer(url)` factory used by cameras/detail.html.
// Falls back to native HLS playback on Safari (which supports .m3u8
// natively without hls.js). Falls back to a friendly error UI when the
// stream URL 404s.

(function () {
  "use strict";

  function fmtError(err) {
    if (!err) return "Unknown stream error";
    if (err.type === "networkError") return "Network error — MediaMTX unreachable";
    if (err.type === "mediaError") return "Media error — codec/manifest issue";
    return String(err);
  }

  function attachHls(videoEl, url, scope) {
    if (window.Hls && window.Hls.isSupported()) {
      const hls = new window.Hls({
        // Reasonable defaults for an NVR live view. Phase 3: expose as
        // Alpine state for the operator to tune.
        liveSyncDurationCount: 3,
        maxBufferLength: 8,
      });
      hls.loadSource(url);
      hls.attachMedia(videoEl);
      hls.on(window.Hls.Events.ERROR, function (_evt, data) {
        if (data.fatal) {
          scope.error = fmtError(data);
          // Try to recover once before giving up.
          switch (data.type) {
            case "networkError":
              hls.startLoad();
              break;
            case "mediaError":
              hls.recoverMediaError();
              break;
            default:
              hls.destroy();
          }
        }
      });
      scope._hls = hls;
    } else if (videoEl.canPlayType("application/vnd.apple.mpegurl")) {
      // Native HLS (Safari).
      videoEl.src = url;
      scope.error = null;
    } else {
      scope.error = "Your browser does not support HLS playback.";
    }
  }

  document.addEventListener("alpine:init", function () {
    window.hlsPlayer = function (url) {
      return {
        error: null,
        init: function () {
          // `init` runs after the DOM is ready and Alpine has bound
          // `this.$refs.video` to the <video> element.
          const videoEl = this.$refs.video;
          if (!videoEl) {
            this.error = "Video element not found.";
            return;
          }
          attachHls(videoEl, url, this);
        },
        destroy: function () {
          if (this._hls) {
            this._hls.destroy();
          }
        },
      };
    };
  });
})();