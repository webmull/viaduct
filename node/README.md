# viaduct-sh (Node client)

Open a [viaduct](https://viaduct.sh) tunnel from Node: give a local port a public
HTTPS URL, with no inbound ports and **zero dependencies** (built on `net`/`tls`).

## Install

```sh
npm install viaduct-sh
```

## Use

```js
import { tunnel } from "viaduct-sh";

const t = await tunnel(3000);
console.log(t.url); // https://funny-otter.viaduct.sh

// ... serve requests ...
await t.close();
```

Options: `tunnel(port, { server, region, tls, poolSize })`. `region` is one of
`lon`, `nyc`, `sg`, `syd`, `blr`; omit it to use the default (London). The tunnel
targets the hosted `viaduct.sh` server by default; point `server` at your own.

Or straight from the terminal, no install:

```sh
npx viaduct-sh 3000
```

MIT licensed. Part of <https://github.com/webmull/viaduct> (the server and Python
client live there too).
