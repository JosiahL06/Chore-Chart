# Icons

Drop-in favicon assets, served at `/icons/<file>`. Replace the placeholder
files with your own art, keeping the same filenames — no HTML edit needed.

| File | Used by | Suggested size |
| --- | --- | --- |
| `favicon.svg` | tab icon (modern browsers) | any (vector) |
| `favicon.ico` | tab icon (fallback, older browsers) | 32×32 |
| `apple-touch-icon.png` | iOS home screen | 180×180 |

The server serves `.svg`, `.ico`, and `.png` from here (see `CONTENT_TYPES`
in `app/server.py`). Placeholders are a paper-colored checkmark on the chart's
moss green (`#5b7359`).
