#!/usr/bin/env node
// npx viaduct-sh <port> [--region lon|nyc|sg|syd|blr] [--server host:port]
import { tunnel, REGIONS } from "../index.js";

function help() {
  console.log(
    `Usage: viaduct-sh <port> [--region ${Object.keys(REGIONS).join("|")}] [--server host:port]`,
  );
}

const args = process.argv.slice(2);
let port;
const opts = {};
for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a === "--region") opts.region = args[++i];
  else if (a === "--server") opts.server = args[++i];
  else if (a === "-h" || a === "--help") { help(); process.exit(0); }
  else if (/^\d+$/.test(a)) port = Number(a);
}

if (!port) { help(); process.exit(1); }

let t;
try {
  t = await tunnel(port, opts);
} catch (err) {
  console.error(`viaduct: ${err.message}`);
  process.exit(1);
}

console.log(`tunnel up  ${t.url}  ->  127.0.0.1:${port}`);
console.log("Ctrl+C to stop");
process.on("SIGINT", async () => {
  await t.close();
  process.exit(0);
});
