import { defineConfig } from "astro/config";
import path from "node:path";

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
      let pending;
      const onChange = (file) => {
        if (!file.startsWith(WHEELS)) return;
        clearTimeout(pending);
        pending = setTimeout(() => server.restart(), 150);
      };

      // A restart, not an invalidate: getStaticPaths caches per route and the
      // supplier data is already baked into the props it handed out, so
      // dropping the data.js module out of the graph would not reach it.
      for (const ev of ["add", "change", "unlink"]) server.watcher.on(ev, onChange);
    },
  };
}

export default defineConfig({
  output: "static",
  vite: { plugins: [watchWheels()] },
});
