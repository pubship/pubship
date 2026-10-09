# PubShip logo assets

The owner supplied the updated mark on 9 October 2026. The original artwork is retained unchanged in `logo-mark.svg`. It has an off-white stroke and a transparent background.

`logo.svg` places that artwork on a charcoal rounded square so it remains visible on both light and dark surfaces. `logo.png` is a 400 x 400 raster rendering of `logo.svg`, used by the README and Cursor plugin. The PNG is rendered at its final size with no artwork upscaling. The source PNG supplied by the owner is 1024 x 1024; the editable vector is the rendering source.

The owner-supplied input files remain untouched. SHA-256:

- Original SVG: `15cb24df5253c6de8e4cdfa4baf8ed0ffffa02bfff4a98556e7f34fc6149030e`
- Original PNG: `377ed81804d5477190d9429b80e97d1690e7ae969fdd0d62f1580435701047c7`

PubShip and its logo are trademarks of Denys Vorobyov. See [TRADEMARKS.md](../TRADEMARKS.md). No Google marks or external assets are included.

## Website sync review

Reviewed [pubship/website at 6524e13](https://github.com/pubship/website/tree/6524e13d147c5b91f60d5c1bc0350d9131f10598) on 9 October 2026:

- Homepage (`site/index.html`): local execution with the developer's own credentials, no PubShip-run service, 170 implemented methods and published version 0.24.1. Logo changes do not change these claims.
- Setup (`site/get-started/index.html`): `uvx pubship`, a local service-account key path, allowed package names and credential-free `--check`. Commands and authentication are unchanged.
- Permissions (`site/permissions/index.html`): reads by default, writes requiring their own opt-ins, Google permissions still enforced, and self-hosting only for the operator's own accounts. No access or data-flow boundary changed.
- Website mark (`site/assets/mark.svg` and the shared header/footer lockup): the site retains its existing variants. This request covers GitHub repository branding and the existing client-manifest logo; it does not replace website artwork.

These exact claims remain consistent with the service. No website update or deployment is included in this PR.
