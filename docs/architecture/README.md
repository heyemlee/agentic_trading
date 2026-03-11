# Architecture diagram

This diagram follows the current human-approval research and execution path. The runtime and tools branch shows research capabilities; it does not grant agents access to broker execution. Audit storage is described in the HTML cards to keep the main path readable.

| File | Purpose |
| --- | --- |
| [system.svg](system.svg) | Canonical Archify SVG export embedded in both READMEs; adapts to light/dark color preference. |
| [system.html](system.html) | Self-contained interactive viewer. Download the file and open it in a browser; GitHub displays HTML source rather than running it. |
| [system.architecture.json](system.architecture.json) | Editable nodes, relationships, layout, and supporting cards. |
| [validation.json](validation.json) | Generator revision, content hashes, showcase results, and browser/visual review status. |

## Rebuild

Generated with [Archify](https://github.com/tt-a1i/archify) at commit `5289f6867f048a7450ec5718f58459613a84cf41`. The diagram retains the repository's English technical labels so both READMEs share one asset.

Clone Archify into a separate tools directory, check out the revision above **in that clone**, and point `ARCHIFY_DIR` at its `archify/` skill directory. From this project's root:

```bash
node "$ARCHIFY_DIR/bin/archify.mjs" validate architecture docs/architecture/system.architecture.json --quality showcase --json
node "$ARCHIFY_DIR/bin/archify.mjs" deliver architecture docs/architecture/system.architecture.json docs/architecture/system.html --quality showcase --json
node "$ARCHIFY_DIR/bin/archify.mjs" visual-check docs/architecture/system.html --json
```

Open the delivered HTML, choose **Export → SVG**, and save the download as `system.svg`. Use the viewer's canonical export so fonts, theme support, and the complete diagram are preserved. Inspect the result and refresh `validation.json` after regenerating; generated visual-check sidecars are review artefacts and need not be committed.

## Main-console screenshot

The top of both READMEs currently uses `docs/screenshots/dashboard-placeholder.svg`. Save your screenshot as `docs/screenshots/dashboard.png`, then replace the image path, alt text, and caption in both READMEs. The separate research screenshot remains a sample-data preview.
