# web/ — owner: Frontend

Suggested: Svelte + Vite (or whatever you are fastest in). Create it here:

```sh
npm create vite@latest . -- --template svelte
npm install
npm run dev
```

Build against the API at http://localhost:8000 (it serves mock data from `mocks/` until the real
endpoints exist). Contract: `../docs/API_CONTRACT.md`.

Map: about 19,000-29,000 points. Use an HTML canvas (d3-scale for coordinates) or deck.gl;
plain SVG will be slow at this size. Time slider: call `/api/timeline?year=...` with a short
debounce. Show the SQL and `took_ms` from `/api/ask` in a small panel (judges like seeing the database work).

Screens, in priority order: 1) map + slider  2) ask box with numbered citations  3) hot/edit-war list
4) then-vs-now view  5) share card (stretch).
