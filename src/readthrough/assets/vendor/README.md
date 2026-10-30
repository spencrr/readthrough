# Vendored libraries

Inlined into the dashboard so it works offline and when opened from disk. Load order matters.

| File | Package | Version | License | SHA-256 |
| --- | --- | --- | --- | --- |
| `cytoscape.min.js` | [cytoscape](https://github.com/cytoscape/cytoscape.js) | 3.34.3 | MIT | `5f3b5b529546d5af1fc5628590af033b74511a5b6f789f5f4682845863228b91` |
| `layout-base.js` | [layout-base](https://github.com/iVis-at-Bilkent/layout-base) | 2.0.1 | MIT | `f5c66be21c9cca16c4f25a576bffbd16ed1a48181c1c2dcbb8d905ec822e76b5` |
| `cose-base.js` | [cose-base](https://github.com/iVis-at-Bilkent/cose-base) | 2.2.0 | MIT | `7cae9509bd36235a63a85e71c8d9fa2cd0bc1d0c1ecc5b5a737976f39d040ddf` |
| `cytoscape-fcose.js` | [cytoscape-fcose](https://github.com/iVis-at-Bilkent/cytoscape.js-fcose) | 2.2.0 | MIT | `4b1cab218d74996aa59cd8473f9239cc6398b8c1774d84d7e59ad9a68959cb57` |
| `marked.umd.js` | [marked](https://github.com/markedjs/marked) | 18.0.14 | MIT | `21568877a938d2c4e7d74e27f18e60da96bb73a68809610ca39216e1efebae62` |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.16 | MPL-2.0 OR Apache-2.0 | `2c90a9b46d6463f26038a29b686e82bc91de01fdac9d5229e7cfe3b360134ea2` |
| `lucide-icons.js` | [lucide-static](https://github.com/lucide-icons/lucide) | 0.469.0 | ISC (portions MIT, from Feather) | `748fbc3ba70c87996157a8eaba8f2bdc5a7f7be20489a1fb3ce599fc39f71aeb` |

The first six files are byte-identical to the files of the same name in the packages' npm tarballs
(`dist/cytoscape.min.js`, `layout-base.js`, `cose-base.js`, `cytoscape-fcose.js`, `lib/marked.umd.js`,
`dist/purify.min.js`); `lucide-icons.js` is readthrough's own subset, holding only the icons the dashboard uses
(the inner markup of each `lucide-static/icons/<name>.svg`). `tests/test_vendor.py` checks every file against
its hash, so a changed file means a changed row, in the same commit, with the new version named.

License texts are in `licenses/`. To update, `npm install` the packages and copy the same files from
`node_modules`, then record the new version and hash here.