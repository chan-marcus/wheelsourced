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

export default defineConfig({
  output: "static",
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
