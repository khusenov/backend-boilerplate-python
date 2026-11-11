import ipaddress

from limits import RateLimitItem, parse
from limits.aio.strategies import MovingWindowRateLimiter

from app.modules.auth.errors import TooManyAttemptsError

LOGINS_PER_ADDRESS = parse("5/hour")
LOGINS_PER_EMAIL = parse("100/hour")
CODE_REQUESTS_PER_ADDRESS = parse("10/hour")
CODE_REQUESTS_PER_EMAIL = parse("5/hour")
GUESSES_PER_ADDRESS = parse("20/hour")
GUESSES_PER_EMAIL = parse("5/15 minutes")
RESET_REQUESTS_PER_ADDRESS = parse("10/hour")
RESET_REQUESTS_PER_EMAIL = parse("5/hour")
RESET_CONFIRMATIONS_PER_ADDRESS = parse("20/hour")


def network_key(client_ip: str | None) -> str:
    try:
        address = ipaddress.ip_address(client_ip or "")
    except ValueError:
        return "unknown"
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    return str(ipaddress.IPv6Network((address, 64), strict=False).network_address)


async def count_attempt(
    limiter: MovingWindowRateLimiter, limit: RateLimitItem, *keys: str
) -> None:
    if not await limiter.hit(limit, *keys):
        raise TooManyAttemptsError


async def count_login_attempt(
    limiter: MovingWindowRateLimiter, client_ip: str | None, email: str
) -> None:
    network = network_key(client_ip)
    await count_attempt(limiter, LOGINS_PER_ADDRESS, "login", network, email)
    await count_attempt(limiter, LOGINS_PER_EMAIL, "login", email)


async def count_code_request(
    limiter: MovingWindowRateLimiter, client_ip: str | None, email: str
) -> None:
    network = network_key(client_ip)
    await count_attempt(limiter, CODE_REQUESTS_PER_ADDRESS, "code_request", network)
    await count_attempt(limiter, CODE_REQUESTS_PER_EMAIL, "code_request", email)


async def count_guess(
    limiter: MovingWindowRateLimiter, client_ip: str | None, email: str
) -> None:
    network = network_key(client_ip)
    await count_attempt(limiter, GUESSES_PER_ADDRESS, "guess", network)
    await count_attempt(limiter, GUESSES_PER_EMAIL, "guess", email)


async def count_reset_request(
    limiter: MovingWindowRateLimiter, client_ip: str | None, email: str
) -> None:
    network = network_key(client_ip)
    await count_attempt(limiter, RESET_REQUESTS_PER_ADDRESS, "reset_request", network)
    await count_attempt(limiter, RESET_REQUESTS_PER_EMAIL, "reset_request", email)


async def count_reset_confirmation(
    limiter: MovingWindowRateLimiter, client_ip: str | None
) -> None:
    network = network_key(client_ip)
    await count_attempt(
        limiter, RESET_CONFIRMATIONS_PER_ADDRESS, "reset_confirmation", network
    )
