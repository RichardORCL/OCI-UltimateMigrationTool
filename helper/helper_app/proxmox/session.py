"""Proxmox login for the web UI: the API address and credentials plus SSH access to the nodes.

Nothing is persisted: the token, password or private key live on the session objects and go
away with the UI session.
"""

from __future__ import annotations

import logging
import shlex
import threading
from typing import Callable, Optional
from urllib.parse import urlsplit

from helper_app.config import Settings
from helper_app.proxmox.client import ProxmoxAuthError, ProxmoxClient, ProxmoxError, parse_pve_url
from helper_app.proxmox.ssh import NodeShell, ShellFactory, SshCredentials, SshError, connect_shell

log = logging.getLogger(__name__)

REQUIRED_TOOLS = ("pvesm", "qemu-img", "qemu-nbd")


class ProxmoxSession:
    """An authenticated Proxmox VE login bound to one UI session."""

    def __init__(self, client: ProxmoxClient, api_host: str, version: str, ssh: SshCredentials,
                 ssh_host: str, node_ips: dict[str, str], shell_factory: ShellFactory, verify_ssl: bool):
        self.client = client
        self.api_host = api_host
        self.version = version
        self.ssh = ssh
        self.ssh_host = ssh_host  # used for every node when set (single node, NAT)
        self.node_ips = dict(node_ips)
        self.verify_ssl = verify_ssl
        self.fingerprints: dict[str, str] = {}
        self._factory = shell_factory
        self._shells: dict[str, NodeShell] = {}
        self._lock = threading.RLock()
        self._closed = False

    @property
    def username(self) -> str:
        return self.client.user

    @property
    def label(self) -> str:
        return f"{self.username} @ {self.api_host} (Proxmox VE {self.version})".replace(" ()", "")

    def node_host(self, node: str) -> str:
        return self.ssh_host or self.node_ips.get(node) or node

    def shell(self, node: str) -> NodeShell:
        """SSH to ``node``; the host key must match the first one seen for that host."""
        host = self.node_host(node)
        with self._lock:
            shell = self._shells.get(host)
            if shell is not None:
                return shell
            shell = self._factory(host, self.ssh, self.fingerprints.get(host, ""))
            self.fingerprints[host] = shell.fingerprint
            self._shells[host] = shell
            return shell

    def sudo(self) -> str:
        return "" if self.ssh.username == "root" else "sudo -n "

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            shells = list(self._shells.values())
            self._shells.clear()
        for shell in shells:
            try:
                shell.close()
            except Exception:  # noqa: BLE001
                pass
        self.client.close()


ClientFactory = Callable[..., ProxmoxClient]


def check_tools(shell: NodeShell, sudo: str) -> None:
    """The node must have the Proxmox storage tool and the QEMU image tools, runnable by the SSH user."""
    names = " ".join(shlex.quote(t) for t in REQUIRED_TOOLS)
    script = f"for t in {names}; do command -v \"$t\" >/dev/null || echo \"missing $t\"; done"
    if sudo:
        script += "; sudo -n true 2>/dev/null || echo 'no passwordless sudo'"
    result = shell.run(f"sh -c {shlex.quote(script)}", timeout=60)
    problems = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.exit_status != 0 and not problems:
        problems.append((result.stderr or f"exit status {result.exit_status}").strip()[:200])
    if problems:
        raise SshError(f"SSH to {shell.host}: " + "; ".join(problems)
                       + " (log in as root or give the user passwordless sudo on a Proxmox node)")


class ProxmoxConnector:
    """Turns the login form into a ``ProxmoxSession``. Both factories are injectable for tests."""

    def __init__(self, settings: Settings, client_factory: Optional[ClientFactory] = None,
                 shell_factory: Optional[ShellFactory] = None):
        self.s = settings
        self._client_factory = client_factory or (lambda base, **kw: ProxmoxClient(base, **kw))
        self._shell_factory = shell_factory or connect_shell

    def login(self, api_url: str, *, auth_mode: str, token_id: str = "", token_secret: str = "",
              username: str = "", password: str = "", ssh_user: str = "", ssh_password: str = "",
              ssh_private_key: str = "", ssh_passphrase: str = "", ssh_host: str = "",
              ssh_port: Optional[int] = None, verify_ssl: bool = False) -> ProxmoxSession:
        base, display = parse_pve_url(api_url)
        if auth_mode == "token":
            if not token_id.strip() or not token_secret.strip():
                raise ProxmoxAuthError("API token ID and secret are required")
            client = self._client_factory(base, token_id=token_id, token_secret=token_secret,
                                          verify_ssl=bool(verify_ssl))
            ssh_user = ssh_user.strip() or "root"
            if not ssh_password and not ssh_private_key.strip():
                raise ProxmoxAuthError("SSH password or private key is required to read the disks")
        elif auth_mode == "password":
            if not username.strip() or not password:
                raise ProxmoxAuthError("user name and password are required")
            client = self._client_factory(base, username=username, password=password, verify_ssl=bool(verify_ssl))
            user, _, realm = username.strip().partition("@")
            ssh_user = ssh_user.strip() or user
            if not ssh_password and not ssh_private_key.strip():
                if realm not in ("", "pam"):
                    raise ProxmoxAuthError(f"{username} is not a Linux account; enter SSH credentials for a node")
                ssh_password = password
        else:
            raise ProxmoxAuthError(f"unknown login mode {auth_mode!r}")
        creds = SshCredentials(username=ssh_user, password=ssh_password, private_key=ssh_private_key.strip(),
                               passphrase=ssh_passphrase, port=int(ssh_port or self.s.proxmox_ssh_port))
        log.info("Proxmox login: %s at %s (%s), SSH as %s", client.user or username, display, auth_mode, ssh_user)
        try:
            version = client.login()
            node_ips = client.node_addresses()
        except ProxmoxError:
            client.close()
            raise
        except Exception as exc:  # noqa: BLE001
            client.close()
            raise ProxmoxError(f"Proxmox login failed: {exc}") from exc
        api_hostname = urlsplit(base).hostname or display
        session = ProxmoxSession(client, display, version, creds, ssh_host.strip(), node_ips,
                                 self._shell_factory, bool(verify_ssl))
        try:
            shell = self._shell_factory(ssh_host.strip() or api_hostname, creds, "")
            session.fingerprints[shell.host] = shell.fingerprint
            session._shells[shell.host] = shell
            check_tools(shell, session.sudo())
        except ProxmoxError:
            session.close()
            raise
        except Exception as exc:  # noqa: BLE001
            session.close()
            raise SshError(f"SSH login failed: {exc}") from exc
        log.info("Proxmox login: %s is Proxmox VE %s, %d node(s), SSH host key %s", display, version,
                 len(node_ips), shell.fingerprint)
        return session
