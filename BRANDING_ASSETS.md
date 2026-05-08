# PCLaw Migrate — branding & icon assets

This repo ships a single-source-of-truth SVG icon used for the website
favicon, the PWA icon, and the Intuit Developer app logo. The same asset
should be uploaded to Intuit Developer so the QuickBooks Online OAuth
consent screen carries the PCLaw Migrate brand.

## Files in `static/`

| File | Where it's used | Notes |
| --- | --- | --- |
| `favicon.svg` | Browser tab favicon (`<link rel="icon">`) and `/favicon.ico` route. | 64×64 viewBox, navy `#1f3b5b` background, white "PM" letterforms. |
| `icon-512.svg` | Apple touch icon, PWA manifest icon, **upload this to Intuit Developer**. | 512×512 viewBox, same design at marketing-grade size. |
| `site.webmanifest` | PWA manifest referenced from `_base.html`. | Theme color `#1f3b5b`, name "PCLaw Migrate". |

## Updating the Intuit OAuth consent screen

Intuit shows a logo on the QuickBooks Online "Connect" / consent page.
That logo is configured **inside the Intuit Developer dashboard** — the
website favicon does not propagate there. Steps:

1. Sign in to <https://developer.intuit.com/> and open the PCLaw Migrate
   app.
2. Go to **Production** → **Keys & OAuth** → **App profile / branding**
   (the exact label changes between Intuit UI revisions; look for the
   "App logo" or "Branding" section).
3. Upload `static/icon-512.svg` (or a 512×512 PNG export of it — see
   below — if Intuit rejects SVG).
4. Set the displayed app name to **PCLaw Migrate**.
5. Repeat for the **Sandbox** app profile so testers also see the
   correct logo.
6. Save and clear your browser cache before re-testing the OAuth flow.

## Exporting a PNG, if Intuit insists on raster

```bash
# requires Inkscape or rsvg-convert
rsvg-convert -w 512 -h 512 static/icon-512.svg -o /tmp/pclaw_migrate_512.png
# or
inkscape static/icon-512.svg --export-type=png --export-width=512 \
  --export-filename=/tmp/pclaw_migrate_512.png
```

Upload the resulting PNG. Keep the SVG as the canonical source — only
re-export when the design changes.

## Changing the brand mark

Both SVGs are intentionally simple. To rebrand:

1. Edit `static/favicon.svg` and `static/icon-512.svg` (paths are
   geometrically identical, just scaled).
2. Update the `theme_color` in `static/site.webmanifest` and the
   `<meta name="theme-color">` value in `templates/_base.html` if you
   change the background color.
3. Re-upload `icon-512.svg` (or its PNG export) to Intuit Developer.
