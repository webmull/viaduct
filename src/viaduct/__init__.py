"""Viaduct — self-hosted reverse tunnel.

Open a tunnel from your own code:

    import viaduct

    async with viaduct.tunnel(8080) as t:
        print(t.url)
"""

from viaduct.api import Tunnel, tunnel, tunnel_sync

__all__ = ["Tunnel", "tunnel", "tunnel_sync"]
