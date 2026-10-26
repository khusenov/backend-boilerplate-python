import ipaddress


def parse_ip(value: str) -> str | None:
    candidate = value.strip()
    if candidate.startswith("["):
        candidate = candidate[1:].partition("]")[0]
    elif candidate.count(":") == 1:
        candidate = candidate.partition(":")[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    return str(address)


def forwarded_client_ip(forwarded_for: str, trusted_hops: int) -> str | None:
    entries = forwarded_for.split(",")
    if trusted_hops == 0 or len(entries) < trusted_hops:
        return None
    return parse_ip(entries[-trusted_hops])


def client_address(
    forwarded_for: str, trusted_hops: int, peer: tuple[str, int] | None
) -> tuple[str, int] | None:
    client_ip = forwarded_client_ip(forwarded_for, trusted_hops)
    if client_ip is not None:
        return client_ip, 0
    if peer is None:
        return None
    host, port = peer
    return parse_ip(host) or host, port
