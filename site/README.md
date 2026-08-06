# Public site

Reads the same YAML that `pipeline.py` writes. No database, no API.

## Layout

Put this folder next to your wheels data:

    aliscrape/
      pipeline.py
      app.py
      wheels/            <- pipeline writes here
        mesh-futura/
          suppliers/*.yaml
      site/              <- this folder
        src/

`src/lib/data.js` resolves `../wheels` at build time.

## Run

    cd site
    npm install
    npm run dev        # http://localhost:4321

## Build and deploy

    npm run build      # -> dist/

Point Cloudflare Pages at the repo: build command `npm run build`,
output directory `site/dist`, root `/`.

## Before publishing

Every row in `src/data/vehicles.yaml` is `verified: false`. Confirm bore and
seat type per chassis and flip the flag. Unverified rows render with a
warning on the page, which is honest but not a substitute for checking.

## Adding a second wheel

`index.astro` renders `wheels[0]`. When you have two, move it to
`src/pages/[wheel].astro` with `getStaticPaths` returning one entry per
wheel slug. The loader already returns them all.
