# SEEKER frontend architecture

The browser application uses native ES modules. It has no runtime dependencies,
bundler, or compilation step.

## Layers

- `core/` owns infrastructure: state, events, HTTP, navigation, DOM helpers,
  and Markdown rendering.
- `components/` owns reusable interaction behavior and may import only `core/`
  or another component.
- `features/` owns product workflows and renderers. Cross-feature dependencies
  must be explicit imports.
- `main.js` is the composition root. It wires features to the static HTML and
  is the only script loaded by `index.html`.

Core and component modules must never import feature modules.

## State

`core/store.js` is the only place application state is defined. Features read
the stable `state` object and change top-level fields through:

```js
updateState({ runId }, 'run:opened');
```

Action names should describe the event that occurred, not the widget that
caused it. Direct `state.property = value` assignments are rejected by the
Python frontend architecture test.

## Browser cache version

The module entry point and every relative JavaScript import carry the same
`?v=N` version. Change all occurrences together when shipping frontend edits.

## Tests

```bash
npm run test:frontend
.venv/bin/pytest tests/test_frontend_assets.py tests/test_directive_roundtrip.py
```

The JavaScript suite exercises pure domain and infrastructure modules. The
Python suite walks the import graph from `main.js` and checks architecture,
selectors, API routes, accessibility contracts, and directive compatibility.

