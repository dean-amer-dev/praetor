"""Network utility helpers — phase 21 condition 4 test fixture."""
import socket


def ping_host(host: str, port: int = 80, timeout: float = 3.0) -> bool:
    """Check if a host is reachable on the given port."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except:
        return False


def resolve_hostname(hostname: str) -> str | None:
    """Resolve hostname to IP, returning None on failure."""
    try:
        return socket.gethostbyname(hostname)
    except:
        return None
