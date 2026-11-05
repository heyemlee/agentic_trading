# Pixel Agents

Source: https://github.com/pixel-agents-hq/pixel-agents
Commit: 3537e140c2094761beae748592aeb92ece8edfdd
Copyright (c) 2026 Pablo De Lucca. MIT license: LICENSE in this directory.

Bundled character and furniture PNGs are copied from webview-ui/public/assets;
of the furniture, only the sprites pixel-office.js loads are kept, without the
upstream manifest.json files.
Upstream credits character designs to JIK-A-4's Metro City character pack.
The preview adapts the direction/frame mapping from spriteData.ts and the
requestAnimationFrame loop from engine/gameLoop.ts. Office layout and preview
integration are local adaptations; this does not run the upstream agent CLI.
