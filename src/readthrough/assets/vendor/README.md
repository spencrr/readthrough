# Vendored libraries

Inlined into the dashboard so it works offline and when opened from disk. Load order matters.

| File | Package | Version | License |
| --- | --- | --- | --- |
| `cytoscape.min.js` | [cytoscape](https://github.com/cytoscape/cytoscape.js) | 3.34.3 | MIT |
| `layout-base.js` | [layout-base](https://github.com/iVis-at-Bilkent/layout-base) | 2.0.1 | MIT |
| `cose-base.js` | [cose-base](https://github.com/iVis-at-Bilkent/cose-base) | 2.2.0 | MIT |
| `cytoscape-fcose.js` | [cytoscape-fcose](https://github.com/iVis-at-Bilkent/cytoscape.js-fcose) | 2.2.0 | MIT |
| `marked.umd.js` | [marked](https://github.com/markedjs/marked) | 18.0.14 | MIT |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.16 | MPL-2.0 OR Apache-2.0 |
| `lucide-icons.js` | [lucide-static](https://github.com/lucide-icons/lucide) | 0.469.0 | ISC (portions MIT, from Feather) |

License texts are in `licenses/`. To update, `npm install` the packages and copy the same files from `node_modules`. `lucide-icons.js` holds only the icons the dashboard uses: the inner markup of each `lucide-static/icons/<name>.svg`.
