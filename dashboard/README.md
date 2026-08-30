# Proxy Service Dashboard

The web UI for SmartProxy. It charts success rate and request volume per proxy
source, for one day at a time, against the Flask API in `src/api/server.py`.

React 19 · TypeScript · Vite 7 · Tailwind CSS 3 · Recharts 3 · Bun.

## Running it

```bash
bun install
bun run dev        # http://localhost:5173, proxying /api to 127.0.0.1:6942
```

The dev server proxies `/api` to the Flask app, so start the proxy service
(`./run.py` at the repository root) alongside it.

```bash
bun run typecheck  # tsc --noEmit against both tsconfigs
bun run lint       # eslint
bun run build      # typecheck, then vite build
```

`build` writes to `../.local/dist`, which Flask serves as its `static_folder`
(`src/api/server.py`). Changing `build.outDir` without changing that path leaves
the production server serving nothing.

## API contract

| Endpoint | Shape |
| --- | --- |
| `GET /api/sources` | `string[]` — `ALL` is added client-side, not returned |
| `GET /api/stats/daily?source&date` | `{ total_requests, total_success, success_rate }` |
| `GET /api/stats/timeseries?source&date&interval` | `{ time, success_rate, total_requests, success_count }[]` |
| `GET /api/stats/overview?date&interval` | `{ sources: { source, daily, timeseries }[] }` |

`interval` is hard-validated server-side to `2 | 5 | 10 | 30 | 60`; the union
type in `src/types/api.ts` mirrors that.

**`success_rate` is `null` for a slot with no traffic.** The backend
deliberately distinguishes "no requests" from "0% success" — every slot of the
day is emitted, so without `null` a line would dive to the floor and run flat
from the current moment to 23:59. Charts pass `connectNulls={false}` so the
line breaks instead. `total_requests` and `success_count` stay `0`.

## Layout

```
src/
  api/client.ts          fetch wrappers: base URL, errors, AbortSignal
  components/            Header, Controls, StatsCards, Charts, ErrorBoundary, …
  hooks/
    useDashboardData.ts  every fetch; owns loading/error/chart state
    useTheme.ts          system preference + localStorage override
  theme/gruvbox.ts       the palette — single source of truth (see below)
  types/api.ts           wire types and the chart row shape
  utils/dateUtils.ts     local-time date helpers (never UTC)
```

Chart rows are `{ time, bySource: Record<string, ChartPoint> }` rather than
dynamic top-level keys, so the shape is expressible without an index signature.

## Theming

`src/theme/gruvbox.ts` holds the Gruvbox light and dark palettes and is the only
place hex values live. `tailwind.config.ts` imports it to

1. emit `--gb-*` custom properties on `:root` (light) and `[data-theme="dark"]`, and
2. map every Tailwind colour utility onto those properties.

So a theme switch is a single attribute flip on `<html>`, and Tailwind classes
follow automatically. Recharts cannot resolve CSS variables for `stroke`/`fill`,
so chart code imports the resolved hex from the same module — same constants, no
drift. Add a colour by adding it to `Palette`; do not hardcode hex elsewhere.

Theme follows `prefers-color-scheme` unless the viewer picks one, which is
persisted in `localStorage` and re-applied before first paint by the inline
script in `index.html`.

Surfaces are separated with **borders (`bg3`), never shadows**: Gruvbox dark's
`bg0 #282828` and `bg1 #3c3836` are too close for shadow-based elevation.
