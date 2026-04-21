"""SaaS tunnel client — connects Merlin to the portal via SSH.

When MERLIN_SAAS_TOKEN is set, opens an SSH connection to the portal
and sets up remote port forwarding so the portal can proxy traffic
to this Merlin instance.

The connection automatically reconnects with exponential backoff
if it drops.
"""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

import asyncssh
from asyncssh.listener import SSHTCPClientListener
from asyncssh.packet import SSHPacket

logger = logging.getLogger("merlin.saas_tunnel")

MERLIN_CLOUD_API = "https://merlincloud.dev"


def _enable_dynamic_port_forwarding(conn: asyncssh.SSHClientConnection) -> None:
    """Accept forwarded-tcpip for any port, not just registered ones.

    The portal uses this to connect to arbitrary ports on the Merlin side
    for on-demand port forwarding (e.g., 6000.my-env.merlincloud.dev).

    Overrides the client's forwarded-tcpip handler to:
    1. Check for exact registered listener (handles the main port 3123 tunnel)
    2. For unregistered ports, create a dynamic forwarder to the requested port
    """

    def _handler(packet: SSHPacket) -> tuple:  # type: ignore[type-arg]
        dest_host = packet.get_string().decode("utf-8")
        dest_port = packet.get_uint32()
        orig_host = packet.get_string().decode("utf-8")
        orig_port = packet.get_uint32()
        packet.check_end()

        # Exact match in registered listeners (e.g., main port tunnel).
        # We only ever register TCP listeners; narrow the union so ty accepts
        # the (orig_host, orig_port) call signature.
        listener = conn._remote_listeners.get((dest_host, dest_port))
        if isinstance(listener, SSHTCPClientListener):
            chan, session = listener.process_connection(orig_host, orig_port)
            logger.info("Forwarded TCP connection on %s:%d", dest_host, dest_port)
            return chan, session

        # Dynamic forwarding: connect to the requested port locally
        chan = conn.create_tcp_channel()
        chan.set_inbound_peer_names(dest_host, dest_port, orig_host, orig_port)
        session = conn.forward_connection(dest_host, dest_port)

        logger.info("Dynamic port forward to %s:%d", dest_host, dest_port)
        return chan, session

    # asyncssh has no public hook for per-connection dynamic port forwarding;
    # overriding the private method is the documented workaround.
    conn._process_forwarded_tcpip_open = _handler  # type: ignore[attr-defined]  # ty: ignore[invalid-assignment]


async def start_saas_tunnel(
    *,
    token: str,
    local_port: int = 3123,
    api_url: str = "",
) -> None:
    """Connect to the portal SSH server and maintain the tunnel.

    Runs forever, reconnecting with exponential backoff on failure.

    Args:
        token: Environment token (mrl_...).
        local_port: Local Merlin HTTP server port to forward.
        api_url: Portal API URL (default: https://merlincloud.dev).
    """
    if not api_url:
        api_url = os.getenv("MERLIN_SAAS_API", MERLIN_CLOUD_API)

    parsed = urlparse(api_url)
    host = parsed.hostname or "merlincloud.dev"
    ssh_port = int(os.getenv("MERLIN_SSH_PORT", "2222"))

    logger.info("SaaS tunnel connecting to %s:%d", host, ssh_port)

    backoff = 1.0
    max_backoff = 30.0

    while True:
        try:
            async with asyncssh.connect(
                host,
                port=ssh_port,
                username="merlin",
                password=token,
                known_hosts=None,
                keepalive_interval=15,
                keepalive_count_max=3,
            ) as conn:
                logger.info("SaaS tunnel connected to %s:%d", host, ssh_port)
                backoff = 1.0  # Reset on success

                # Set up remote port forwarding:
                # Portal listens on a random port, forwards to our localhost:local_port
                listener = await conn.forward_remote_port(
                    "127.0.0.1", 0, "127.0.0.1", local_port
                )
                fwd_port = listener.get_port()
                logger.info(
                    "Port forwarding active: portal:%d -> localhost:%d",
                    fwd_port,
                    local_port,
                )

                # Enable dynamic port forwarding for on-demand port access
                _enable_dynamic_port_forwarding(conn)
                logger.info("Dynamic port forwarding enabled")

                # Block until the connection is closed
                await conn.wait_closed()

        except asyncssh.PermissionDenied:
            logger.error(
                "SaaS tunnel auth failed — token was revoked or regenerated.\n"
                "Get your new connect command from merlincloud.dev and run:\n"
                "  merlin --saas-token <new-token>"
            )
            return  # Don't retry auth failures

        except (OSError, asyncssh.DisconnectError, asyncssh.ConnectionLost) as e:
            logger.warning("SaaS tunnel disconnected: %s", e)

        except Exception:
            logger.exception("SaaS tunnel unexpected error")

        logger.info("Reconnecting in %.0fs...", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)
