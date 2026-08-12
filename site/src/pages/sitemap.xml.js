// Hand-rolled rather than @astrojs/sitemap. The URL set is the two static
// pages plus one per wheel, which is a dozen lines to generate and does not
// justify a dependency in a project whose whole build is astro + js-yaml.
//
// /stats is deliberately absent. It is the password-gated admin view and
// holds submitters' email addresses; listing it would be inviting crawlers at
// the one page on the site that should not be found.
import { loadWheels } from "../lib/data.js";

export function GET({ site }) {
  const base = String(site).replace(/\/$/, "");
  const wheels = loadWheels();

  // lastmod is the newest verification date among a wheel's suppliers, which
  // is a real date this data was checked. Omitted rather than guessed when
  // nothing carries one: a made-up lastmod trains crawlers to ignore it.
  const lastmodOf = (w) => {
    const dates = w.suppliers.map((s) => s.verified_on).filter(Boolean).sort();
    return dates.length ? dates[dates.length - 1] : null;
  };

  const urls = [
    { loc: `${base}/`, priority: "1.0" },
    ...wheels.map((w) => ({
      loc: `${base}/${w.slug}/`,
      priority: "0.9",
      lastmod: lastmodOf(w),
    })),
    { loc: `${base}/request/`, priority: "0.5" },
  ];

  const body = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.map((u) => `  <url>
    <loc>${u.loc}</loc>${u.lastmod ? `
    <lastmod>${u.lastmod}</lastmod>` : ""}
    <priority>${u.priority}</priority>
  </url>`).join("\n")}
</urlset>
`;

  return new Response(body, {
    headers: { "Content-Type": "application/xml; charset=utf-8" },
  });
}
