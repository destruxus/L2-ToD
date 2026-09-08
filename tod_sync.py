"""Wire protocol for multi-bot TOD sync (compatible with Ally1 / Ally2 TOD bots).

This module is transport only. It does not change L2-ToD timer logic.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout, web

logger = logging.getLogger("l2-tod-sync")

SIGNATURE_HEADER = "X-TOD-Sync-Signature"
ORIGIN_HEADER = "X-TOD-Sync-Origin"
SYNC_PATH = "/v1/tod-sync"

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
OriginJsonProvider = Callable[[str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class SyncPeer:
    origin: str
    name: str
    url: str


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "peer"


def origin_slug(value: str) -> str:
    return _slug(value)


def peer_display_name(origin: str, peers: Iterable[SyncPeer], local_origin: str, local_name: str) -> str:
    if origin == local_origin and local_name:
        return local_name
    for peer in peers:
        if peer.origin == origin:
            return peer.name
    return origin or "unknown"


def sign_body(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def signature_valid(secret: str, body: bytes, header_value: str) -> bool:
    expected = sign_body(secret, body)
    provided = (header_value or "").strip()
    return hmac.compare_digest(expected, provided)


def parse_peer_endpoints(raw: str) -> list[str]:
    """Parse extra peer base URLs. Each entry must include an explicit port.

    Accepts comma, semicolon, or whitespace separated values, for example:

        http://203.0.113.10:8081,http://203.0.113.11:8082
        10.0.0.5:8083

    Host ports used by the known compose files:
        Ally1     8081  (container 8080)
        Ally2     8082  (container 8080)
        L2-ToD    8083  (container 8080)
    """
    urls: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,;]+", (raw or "").strip()):
        if not token:
            continue
        candidate = token.rstrip("/")
        if "://" not in candidate:
            candidate = f"http://{candidate}"
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            logger.warning("Ignoring TOD sync peer (need http(s) host:port): %s", token)
            continue
        if parsed.port is None:
            logger.warning(
                "Ignoring TOD sync peer %s — include the exact host port "
                "(Ally1 8081, Ally2 8082, L2-ToD 8083).",
                token,
            )
            continue
        url = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


class TodSyncBridge:
    def __init__(
        self,
        *,
        origin: str,
        name: str,
        secret: str,
        host: str,
        port: int,
        handler: Handler,
        info_provider: OriginJsonProvider,
        latest_provider: OriginJsonProvider,
        fetch_pending: Callable[[], list[tuple[int, dict[str, Any]]]],
        mark_delivered: Callable[[int], None],
        mark_attempt: Callable[[int, str], None],
        extra_urls: Iterable[str] | None = None,
    ) -> None:
        self.origin = origin
        self.name = name or origin
        self.secret = secret
        self.peers: list[SyncPeer] = []
        self.host = host
        self.port = port
        self.extra_urls = list(extra_urls or [])
        self._handler = handler
        self._info_provider = info_provider
        self._latest_provider = latest_provider
        self._fetch_pending = fetch_pending
        self._mark_delivered = mark_delivered
        self._mark_attempt = mark_attempt
        self._runner: web.AppRunner | None = None
        self._session: ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.origin and self.secret)

    def peer_by_origin(self, origin: str) -> SyncPeer | None:
        for peer in self.peers:
            if peer.origin == origin:
                return peer
        return None

    async def start(self) -> None:
        if not self.enabled:
            logger.info("TOD peer sync is off (set TOD_SYNC_ORIGIN and TOD_SYNC_SECRET to enable).")
            return
        app = web.Application()
        app.router.add_get(f"{SYNC_PATH}/health", self._health)
        app.router.add_get(f"{SYNC_PATH}/info", self._info)
        app.router.add_get(f"{SYNC_PATH}/latest", self._latest)
        app.router.add_post(SYNC_PATH, self._receive)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self._session = ClientSession(timeout=ClientTimeout(total=10))
        logger.info(
            "TOD sync listening on %s:%s as %s (%s); path %s",
            self.host,
            self.port,
            self.name,
            self.origin,
            SYNC_PATH,
        )

    async def discover_peers(
        self,
        host: str,
        ports: Iterable[int],
        extra_urls: Iterable[str] | None = None,
    ) -> list[SyncPeer]:
        """Probe host:port health endpoints plus extra full URLs and replace the live peer list."""
        if self._session is None:
            return list(self.peers)
        urls = [f"http://{host}:{int(port)}" for port in ports]
        extras = list(extra_urls) if extra_urls is not None else list(self.extra_urls)
        for url in extras:
            if url not in urls:
                urls.append(url)
        probes = [self._probe_url(url) for url in urls]
        results = await asyncio.gather(*probes, return_exceptions=True)
        found: list[SyncPeer] = []
        seen: set[str] = set()
        for result in results:
            if isinstance(result, BaseException) or result is None:
                continue
            if result.origin == self.origin or result.origin in seen:
                continue
            seen.add(result.origin)
            found.append(result)
        found.sort(key=lambda peer: (peer.name.lower(), peer.origin))
        self.peers = found
        return found

    async def _probe_peer(self, host: str, port: int) -> SyncPeer | None:
        return await self._probe_url(f"http://{host}:{port}")

    async def _probe_url(self, url: str) -> SyncPeer | None:
        if self._session is None:
            return None
        base = url.rstrip("/")
        timeout = ClientTimeout(total=2)
        try:
            async with self._session.get(f"{base}{SYNC_PATH}/health", timeout=timeout) as response:
                if response.status >= 400:
                    return None
                text = await response.text()
                data = json.loads(text) if text else {}
        except (ClientError, TimeoutError, asyncio.TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict) or not data.get("ok"):
            return None
        origin = _slug(str(data.get("origin") or ""))
        if not origin:
            return None
        name = str(data.get("name") or origin).strip() or origin
        return SyncPeer(origin=origin, name=name, url=base)

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def flush_outbox(self) -> None:
        if not self.enabled or not self.peers or self._session is None:
            return
        for row_id, payload in self._fetch_pending():
            try:
                await self._post_to_peers(payload)
            except Exception as exc:
                self._mark_attempt(row_id, str(exc))
                logger.warning("TOD sync delivery failed for outbox %s: %s", row_id, exc)
            else:
                self._mark_delivered(row_id)

    async def fetch_info(self, peer: SyncPeer) -> dict[str, Any]:
        return await self._signed_get(f"{peer.url}{SYNC_PATH}/info")

    async def fetch_latest(self, peer: SyncPeer) -> dict[str, Any]:
        return await self._signed_get(f"{peer.url}{SYNC_PATH}/latest")

    async def _signed_get(self, url: str) -> dict[str, Any]:
        if self._session is None:
            return {"ok": False, "error": "sync_not_started"}
        headers = {
            SIGNATURE_HEADER: sign_body(self.secret, b""),
            ORIGIN_HEADER: self.origin,
        }
        try:
            async with self._session.get(url, headers=headers) as response:
                text = await response.text()
                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    data = {"ok": False, "error": text[:200]}
                if not isinstance(data, dict):
                    return {"ok": False, "error": "invalid_payload", "status": response.status}
                data.setdefault("ok", response.status < 400)
                data["status"] = response.status
                return data
        except ClientError as exc:
            return {"ok": False, "error": str(exc)}

    async def _post_to_peers(self, payload: dict[str, Any]) -> None:
        if self._session is None:
            raise RuntimeError("sync session is not started")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign_body(self.secret, body),
            ORIGIN_HEADER: self.origin,
        }
        errors: list[str] = []
        for peer in self.peers:
            url = f"{peer.url}{SYNC_PATH}"
            try:
                async with self._session.post(url, data=body, headers=headers) as response:
                    text = await response.text()
                    if response.status >= 400:
                        errors.append(f"{peer.name} -> {response.status} {text[:200]}")
                        continue
                    try:
                        data = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        data = {}
                    skipped = data.get("skipped") if isinstance(data, dict) else ""
                    if skipped in {"receive_disabled", "not_allowed"}:
                        errors.append(f"{peer.name} -> {skipped}")
            except ClientError as exc:
                errors.append(f"{peer.name} -> {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    async def _require_signature(self, request: web.Request, body: bytes) -> web.Response | None:
        if not signature_valid(self.secret, body, request.headers.get(SIGNATURE_HEADER, "")):
            return web.json_response({"ok": False, "error": "invalid_signature"}, status=401)
        return None

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "origin": self.origin, "name": self.name})

    async def _info(self, request: web.Request) -> web.Response:
        denied = await self._require_signature(request, b"")
        if denied:
            return denied
        requester = request.headers.get(ORIGIN_HEADER, "")
        return web.json_response(await self._info_provider(requester))

    async def _latest(self, request: web.Request) -> web.Response:
        denied = await self._require_signature(request, b"")
        if denied:
            return denied
        requester = request.headers.get(ORIGIN_HEADER, "")
        payload = await self._latest_provider(requester)
        status = 200 if payload.get("ok", True) else int(payload.get("status") or 403)
        return web.json_response(payload, status=status)

    async def _receive(self, request: web.Request) -> web.Response:
        body = await request.read()
        denied = await self._require_signature(request, body)
        if denied:
            return denied
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "invalid_payload"}, status=400)

        origin = str(payload.get("origin") or "")
        header_origin = request.headers.get(ORIGIN_HEADER, "")
        if not origin or origin != header_origin:
            return web.json_response({"ok": False, "error": "origin_mismatch"}, status=400)
        if origin == self.origin:
            return web.json_response({"ok": True, "skipped": "self"})

        return web.json_response({"ok": True, "skipped": "pull_only"})
