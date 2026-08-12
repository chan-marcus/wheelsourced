import { defineConfig } from "astro/config";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

// loadWheels() reads ../wheels, which is outside Vite's root, and Vite only
// watches its own root. So nothing the GUI wrote ever invalidated the dev
// server: it kept serving whatever it read at startup while the GUI, which
// reads YAML per request, showed the truth. The two disagreeing reads as "the
// reset did not work" rather than "this page is stale", which is the expensive
// kind of wrong.
const WHEELS = path.resolve("../wheels");

function watchWheels() {
  return {
    name: "watch-wheels",
    configureServer(server) {
      server.watcher.add(WHEELS);

      // A pull rewrites a dozen YAML files in a burst, so coalesce rather than
      // restarting once per file.
      //
      // `restarting` is the other half of that, and it is not optional. A
      // restart that begins while one is still finishing races it for the
      // port: the second server finds 4321 held by the first, takes 4322
      // instead, and the first leaves a socket behind that listens and
      // answers nothing. You are then looking at a dead 4321 and a live 4322
      // and no reason to suspect the difference. Overlapping restarts are the
      // cause; serialising them is the fix.
      let pending;
      let restarting = false;
      const onChange = (file) => {
        if (!file.startsWith(WHEELS)) return;
        clearTimeout(pending);
        pending = setTimeout(async () => {
          if (restarting) return;
          restarting = true;
          try {
            await server.restart();
          } finally {
            restarting = false;
          }
        }, 150);
      };

      // A restart, not an invalidate: getStaticPaths caches per route and the
      // supplier data is already baked into the props it handed out, so
      // dropping the data.js module out of the graph would not reach it.
      for (const ev of ["add", "change", "unlink"]) server.watcher.on(ev, onChange);
    },
  };
}

// loadWheels() mirrors each wheel's image out of ../wheels and into public/,
// and it does that while getStaticPaths runs. Astro has copied public/ into
// dist/ by then, so on a clean checkout the images never reach the build at
// all and every wheel page 404s its own photo.
//
// It survives locally only by accident: the file is left behind in public/,
// which is gitignored, and the NEXT build copies it. So the machine that has
// built before is the one machine where this cannot be reproduced, and CI is
// guaranteed to hit it on the first deploy.
//
// Copy again once dist exists. astro:build:done is the only hook late enough
// to be sure both the mirror and the output directory are there.
function mirrorWheelImages() {
  return {
    name: "mirror-wheel-images",
    hooks: {
      "astro:build:done": ({ dir, logger }) => {
        const from = path.resolve("./public/wheels");
        if (!fs.existsSync(from)) return;
        const to = path.join(fileURLToPath(dir), "wheels");
        fs.cpSync(from, to, { recursive: true });
        const n = fs.readdirSync(to).length;
        logger.info(`mirrored ${n} wheel image folder(s) into dist/wheels`);
      },
    },
  };
}

export default defineConfig({
  // Needed for absolute URLs: canonical tags, og:url and the sitemap all have
  // to name a full origin, and Astro has no way to guess one. Apex rather
  // than www because both currently answer 200 with the same content, which
  // splits every page's ranking signals across two URLs; the canonical tags
  // this enables are what consolidate them.
  site: "https://wheelsourced.com",
  output: "static",
  integrations: [mirrorWheelImages()],
  server: {
    // Bind IPv4 too. Node resolves "localhost" to ::1 first on macOS, so the
    // default bind answered localhost:4321 but not 127.0.0.1:4321, which is
    // the form anything hardcoding an address tends to use.
    host: "127.0.0.1",
    port: 4321,
  },
  vite: {
    plugins: [watchWheels()],
    // Never silently move to another port. Drifting is how the sheet ends up
    // being served somewhere other than where it is being looked for; better
    // to fail at startup and say the port is taken.
    server: { strictPort: true },
  },
});
