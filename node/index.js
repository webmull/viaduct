// viaduct-sh — open a viaduct tunnel from Node. Zero dependencies (net/tls only).
//
//   import { tunnel } from "viaduct-sh";
//   const t = await tunnel(3000);
//   console.log(t.url);          // https://funny-otter.viaduct.sh
//   // ... serve requests ...
//   await t.close();
//
// The wire protocol matches the Python client: 4-byte big-endian length +
// UTF-8 JSON frames for the handshake, then raw bytes on the data path.

import net from "node:net";
import tls from "node:tls";

export const REGIONS = {
  lon: "viaduct.sh:4443",
  nyc: "nyc.viaduct.sh:4443",
  sg: "sg.viaduct.sh:4443",
  syd: "syd.viaduct.sh:4443",
  blr: "blr.viaduct.sh:4443",
};

const DEFAULT_SERVER = "viaduct.sh:4443";
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);
const DEFAULT_POOL_SIZE = 10;

const encodeFrame = (obj) => {
  const payload = Buffer.from(JSON.stringify(obj), "utf8");
  const header = Buffer.allocUnsafe(4);
  header.writeUInt32BE(payload.length, 0);
  return Buffer.concat([header, payload]);
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function resolveServer(opts) {
  let server = opts.server;
  if (opts.region) {
    if (server) throw new Error("pass either server or region, not both");
    server = REGIONS[String(opts.region).toLowerCase()];
    if (!server) throw new Error(`unknown region ${opts.region}; try ${Object.keys(REGIONS).join(", ")}`);
  }
  server = server || DEFAULT_SERVER;
  const i = server.lastIndexOf(":");
  if (i === -1) throw new Error(`server must be host:port, got ${server}`);
  const host = server.slice(0, i);
  const port = Number(server.slice(i + 1));
  const useTls = opts.tls === undefined ? !LOCAL_HOSTS.has(host) : Boolean(opts.tls);
  return { host, port, useTls };
}

const dial = ({ host, port, useTls }) =>
  useTls ? tls.connect({ host, port, servername: host }) : net.connect({ host, port });

// Wait for the socket's first inbound chunk (an assignment) or its close.
// Pauses the socket on the first chunk so no request bytes are lost before the
// pipe is wired up. Resolves with the chunk, or null if the connection dropped.
function firstChunk(sock) {
  return new Promise((resolve) => {
    const done = (v) => {
      sock.off("data", onData);
      sock.off("close", onEnd);
      sock.off("error", onEnd);
      resolve(v);
    };
    const onData = (chunk) => { sock.pause(); done(chunk); };
    const onEnd = () => done(null);
    sock.on("data", onData);
    sock.on("close", onEnd);
    sock.on("error", onEnd);
  });
}

// Splice an assigned tunnel socket to the local app; resolve when both are done.
function serve(sock, first, localPort) {
  return new Promise((resolve) => {
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      sock.destroy();
      local.destroy();
      resolve();
    };
    const local = net.connect({ host: "127.0.0.1", port: localPort });
    local.once("error", () => {
      const body = "The tunnel is up, but nothing is listening on the local port.";
      sock.write(
        `HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain; charset=utf-8\r\n` +
        `Content-Length: ${Buffer.byteLength(body)}\r\nConnection: close\r\n\r\n${body}`,
      );
      finish();
    });
    local.once("connect", () => {
      local.write(first);
      sock.resume();
      sock.pipe(local);
      local.pipe(sock);
    });
    sock.once("close", finish);
    sock.once("error", finish);
    local.once("close", finish);
  });
}

// One pool slot: keep an idle data connection at the server; serve on assignment,
// then loop. Reconnects with backoff on failure.
async function dataSlot(state) {
  let backoff = 1000;
  while (!state.closing) {
    const sock = dial(state.conn);
    state.sockets.add(sock);
    try {
      sock.on("error", () => {}); // handled via firstChunk/serve resolution
      sock.write(encodeFrame({ type: "data_hello", subdomain: state.subdomain, ...(state.token ? { token: state.token } : {}) }));
      const first = await firstChunk(sock);
      if (first === null) {
        sock.destroy();
        if (!state.closing) await sleep(backoff), (backoff = Math.min(backoff * 2, 30000));
        continue;
      }
      backoff = 1000;
      await serve(sock, first, state.localPort);
    } catch {
      sock.destroy();
      if (!state.closing) await sleep(backoff), (backoff = Math.min(backoff * 2, 30000));
    } finally {
      state.sockets.delete(sock);
    }
  }
}

/**
 * Open a tunnel exposing local `port`. Resolves with `{ url, hostname, subdomain, close() }`.
 * Options: `server`, `region`, `tls`, `poolSize`.
 */
export async function tunnel(port, opts = {}) {
  const conn = resolveServer(opts);
  const poolSize = opts.poolSize || DEFAULT_POOL_SIZE;
  const state = { conn, closing: false, subdomain: null, token: null, localPort: port, sockets: new Set() };

  const control = dial(conn);
  state.control = control;

  const ok = await new Promise((resolve, reject) => {
    let buf = Buffer.alloc(0);
    let gotOk = false;
    control.once("error", reject);
    control.once("close", () => {
      state.closing = true;
      if (!gotOk) reject(new Error("connection closed during handshake"));
    });
    control.on("data", (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      while (buf.length >= 4) {
        const len = buf.readUInt32BE(0);
        if (buf.length < 4 + len) break;
        let frame;
        try {
          frame = JSON.parse(buf.subarray(4, 4 + len).toString("utf8"));
        } catch {
          control.destroy();
          return;
        }
        buf = buf.subarray(4 + len);
        if (!gotOk) {
          gotOk = true;
          if (frame.type === "ok") resolve(frame);
          else reject(new Error(frame.reason || `unexpected reply ${frame.type}`));
        } else if (frame.type === "ping") {
          control.write(encodeFrame({ type: "pong" }));
        }
      }
    });
    control.write(encodeFrame({ type: "hello", local_port: port, ...(opts.pin ? { pin: opts.pin } : {}) }));
  });

  const hostname = ok.hostname;
  state.subdomain = hostname.split(".")[0];
  state.token = ok.token || null;

  for (let i = 0; i < poolSize; i++) dataSlot(state);

  return {
    url: `https://${hostname}`,
    hostname,
    subdomain: state.subdomain,
    async close() {
      state.closing = true;
      control.destroy();
      for (const s of state.sockets) s.destroy();
    },
  };
}

export default { tunnel, REGIONS };
