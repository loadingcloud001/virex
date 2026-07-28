# Self-hosting frontend assets (Phase 3)

The portal currently loads HTMX, Alpine.js, Tailwind, and DaisyUI from
`cdn.jsdelivr.net` — see `templates/base.html` for the exact `<script>` /
`<link>` tags. This is fine for Phase 1 (operator-driven dev/pilot portal
behind a Cloudflare Tunnel that already has internet egress), but if
egress becomes a concern you can swap to fully self-hosted assets without
any other code change.

## Procedure

1. Download the pinned versions into this directory:

   ```bash
   cd portal/static/vendor
   curl -L -o htmx.min.js \
     https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js
   curl -L -o alpine.min.js \
     https://cdn.jsdelivr.net/npm/alpinejs@3.14.9/dist/cdn.min.js
   curl -L -o tailwind.css \
     'https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4/dist/index.css'
   curl -L -o daisyui.css \
     https://cdn.jsdelivr.net/npm/daisyui@5.7.4/dist/full.min.css
   ```

   Or precompile the Tailwind v4 + DaisyUI v5 bundle offline with the
   Tailwind CLI (`npx @tailwindcss/cli`) and skip the JIT browser step
   entirely; this dramatically reduces runtime CSS size.

2. In `templates/base.html`, replace the CDN URLs with `/static/vendor/`
   paths:

   ```diff
   - <script src="https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js" defer></script>
   + <script src="/static/vendor/htmx.min.js" defer></script>
   ```

3. Tighten the CSP in `app.py` to drop `cdn.jsdelivr.net` and
   `'unsafe-inline'` (move any inline `<script>` into a static file with
   a CSP hash).

4. Verify: `curl -I http://127.0.0.1:8765/login` returns 200 + the
   `<link rel="stylesheet" href="/static/vendor/daisyui.css">` tag.

5. Run `portal/tests/test_integration.py` — the CSP test asserts the
   header is set; update it if you tighten the directive list.

## Why not do this in Phase 1?

- Adds a Node.js dependency (Tailwind CLI) to the build chain for a
  design that's still being iterated on.
- ~3 MB Tailwind Play CDN hit only affects first-page paint; subsequent
  navigations are cached.
- The portal is internal (Cloudflare Tunnel + operator-only). Bad actors
  who could MITM a CDN response already have bigger problems.

## Version pinning

Pinned versions match `templates/base.html`. When you bump a dependency,
update both this file and `base.html` together.
