import fs from "node:fs";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { JSDOM } = require("jsdom");
const readabilityPkg = require("@mozilla/readability");
const Readability = readabilityPkg.Readability || readabilityPkg.default || readabilityPkg;

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const out = [];
for (const row of input) {
  try {
    const dom = new JSDOM(row.html, { url: row.url });
    const parsed = new Readability(dom.window.document).parse();
    out.push({
      url: row.url,
      title: parsed?.title || "",
      text: parsed?.textContent || "",
      html: parsed?.content || "",
      byline: parsed?.byline || "",
      length: parsed?.length || 0,
    });
  } catch (err) {
    out.push({ url: row.url, error: String(err), text: "", html: "" });
  }
}
process.stdout.write(JSON.stringify(out));
