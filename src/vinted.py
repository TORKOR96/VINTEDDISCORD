"""
Vinted API client — accès non authentifié via session cookie.

Stratégie anti-ban :
  - Rotation des User-Agents
  - Refresh automatique des cookies toutes les heures
  - Backoff automatique sur 429 / 5xx
  - Timeout strict par requête
  - Une requête à la fois par domaine (lock asyncio)
  - Cache mémoire pour les suggestions (marques, catégories, tailles)
"""

import asyncio
import logging
import random
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

_COOKIE_TTL = 3600        # secondes avant refresh cookie
_RATE_LIMIT_BACKOFF = 60  # secondes d'attente après 429
_SUGGEST_CACHE_TTL = 300  # secondes de cache pour suggestions


class _DomainState:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.cookie_ts: float = 0.0
        self.lock = asyncio.Lock()
        self.backoff_until: float = 0.0


class VintedClient:
    def __init__(self):
        self._domains: dict[str, _DomainState] = {}
        # Cache des suggestions : clé → (timestamp, résultat)
        self._suggest_cache: dict[str, tuple[float, list]] = {}
        # Lock léger pour les appels de suggestion (pas de file d'attente globale)
        self._suggest_lock = asyncio.Semaphore(3)

    # ── Recherche d'articles ───────────────────────────────────────────────────

    async def search(self, domain: str, params: dict) -> Optional[list[dict]]:
        state = self._get_state(domain)
        async with state.lock:
            remaining = state.backoff_until - time.monotonic()
            if remaining > 0:
                logger.info(f"[{domain}] Backoff actif, attente {remaining:.0f}s")
                await asyncio.sleep(remaining)
            await self._ensure_session(domain, state)
            return await self._do_search(domain, state, params)

    # ── Suggestions pour l'autocomplete ───────────────────────────────────────

    async def search_brands(
        self, query: str, domain: str = "www.vinted.fr"
    ) -> list[dict]:
        """
        Renvoie les marques dont le nom contient `query`.
        Résultat : [{"id": int, "title": str}, ...]
        Cache TTL = 5 min.
        """
        if len(query) < 2:
            return []

        cache_key = f"brands:{domain}:{query.lower()}"
        cached = self._from_cache(cache_key)
        if cached is not None:
            return cached

        url = f"https://{domain}/api/v2/brands"
        data = await self._suggest_get(domain, url, {"search_text": query, "per_page": 25})
        if data is None:
            return []

        brands = [
            {"id": b["id"], "title": b["title"]}
            for b in data.get("brands", [])
            if b.get("id") and b.get("title")
        ]
        self._to_cache(cache_key, brands)
        return brands

    async def get_catalogs(self, domain: str = "www.vinted.fr") -> list[dict]:
        """
        Retourne les catégories principales depuis l'API Vinted.
        Résultat : [{"id": int, "title": str}, ...]
        """
        cache_key = f"catalogs:{domain}"
        cached = self._from_cache(cache_key)
        if cached is not None:
            return cached

        url = f"https://{domain}/api/v2/catalog"
        data = await self._suggest_get(domain, url, {"per_page": 100})
        if data is None:
            return _FALLBACK_CATALOGS

        catalogs: list[dict] = []
        for cat in data.get("catalogs", []):
            if cat.get("id") and cat.get("title"):
                catalogs.append({"id": cat["id"], "title": cat["title"]})
                for sub in cat.get("catalogs", []):
                    if sub.get("id") and sub.get("title"):
                        catalogs.append({
                            "id": sub["id"],
                            "title": f"  └ {sub['title']}",
                        })

        result = catalogs or _FALLBACK_CATALOGS
        self._to_cache(cache_key, result)
        return result

    async def get_sizes(
        self, query: str = "", catalog_id: Optional[int] = None, domain: str = "www.vinted.fr"
    ) -> list[dict]:
        """
        Retourne les tailles disponibles, filtrées par catalog_id si fourni.
        Résultat : [{"id": int, "title": str}, ...]
        """
        cache_key = f"sizes:{domain}:{catalog_id}"
        cached = self._from_cache(cache_key)
        if cached is None:
            params: dict = {"per_page": 200}
            if catalog_id:
                params["catalog_id"] = catalog_id
            url = f"https://{domain}/api/v2/sizes"
            data = await self._suggest_get(domain, url, params)

            if data is None:
                cached = _FALLBACK_SIZES
            else:
                sizes = [
                    {"id": s["id"], "title": s.get("title", s.get("name", ""))}
                    for s in data.get("sizes", [])
                    if s.get("id")
                ]
                cached = sizes or _FALLBACK_SIZES

            self._to_cache(cache_key, cached)

        if query:
            q = query.lower()
            return [s for s in cached if q in s["title"].lower()]
        return cached

    # ── Fermeture ─────────────────────────────────────────────────────────────

    async def close(self):
        for state in self._domains.values():
            if state.session:
                await state.session.close()
        self._domains.clear()

    # ── Helpers internes ──────────────────────────────────────────────────────

    def _get_state(self, domain: str) -> _DomainState:
        if domain not in self._domains:
            self._domains[domain] = _DomainState()
        return self._domains[domain]

    def _base_headers(self) -> dict:
        return {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    async def _ensure_session(self, domain: str, state: _DomainState):
        now = time.monotonic()
        if state.session and (now - state.cookie_ts) < _COOKIE_TTL:
            return

        if state.session:
            await state.session.close()
            state.session = None

        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=True, limit=4),
            cookie_jar=aiohttp.CookieJar(),
            headers={"User-Agent": random.choice(_USER_AGENTS)},
        )
        try:
            async with session.get(
                f"https://{domain}",
                headers=self._base_headers(),
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                logger.info(f"[{domain}] Session initialisée (status={resp.status})")
        except Exception as exc:
            logger.warning(f"[{domain}] Init session : {exc}")

        state.session = session
        state.cookie_ts = now

    async def _do_search(self, domain: str, state: _DomainState, user_params: dict) -> Optional[list[dict]]:
        api_params: dict = {"order": "newest_first", "per_page": 20, "page": 1}
        for k, v in user_params.items():
            if v is not None and v != "" and v != []:
                api_params[k] = v

        url = f"https://{domain}/api/v2/catalog/items"
        try:
            async with state.session.get(
                url,
                params=api_params,
                headers={
                    **self._base_headers(),
                    "Referer": f"https://{domain}/catalog",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    items = data.get("items", [])
                    logger.debug(f"[{domain}] {len(items)} articles récupérés")
                    return items
                if resp.status == 429:
                    wait = max(int(resp.headers.get("Retry-After", _RATE_LIMIT_BACKOFF)), _RATE_LIMIT_BACKOFF)
                    logger.warning(f"[{domain}] 429 — attente {wait}s")
                    state.backoff_until = time.monotonic() + wait
                    return None
                if resp.status in (401, 403):
                    logger.warning(f"[{domain}] {resp.status} — refresh cookie forcé")
                    state.cookie_ts = 0.0
                    return None
                if resp.status >= 500:
                    state.backoff_until = time.monotonic() + 30
                    return None
                logger.warning(f"[{domain}] status inattendu {resp.status}")
                return None
        except asyncio.TimeoutError:
            logger.warning(f"[{domain}] Timeout")
            return None
        except Exception as exc:
            logger.error(f"[{domain}] Erreur : {exc}")
            return None

    async def _suggest_get(self, domain: str, url: str, params: dict) -> Optional[dict]:
        """GET léger pour les suggestions (autocomplete). Pas de lock global."""
        state = self._get_state(domain)
        await self._ensure_session(domain, state)

        async with self._suggest_lock:
            try:
                async with state.session.get(
                    url,
                    params=params,
                    headers={
                        **self._base_headers(),
                        "Referer": f"https://{domain}/",
                    },
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    logger.debug(f"[suggest] {url} → {resp.status}")
                    return None
            except Exception as exc:
                logger.debug(f"[suggest] Erreur : {exc}")
                return None

    def _from_cache(self, key: str) -> Optional[list]:
        entry = self._suggest_cache.get(key)
        if entry and (time.monotonic() - entry[0]) < _SUGGEST_CACHE_TTL:
            return entry[1]
        return None

    def _to_cache(self, key: str, value: list):
        self._suggest_cache[key] = (time.monotonic(), value)


# ── Fallbacks si l'API ne répond pas ──────────────────────────────────────────

_FALLBACK_CATALOGS = [
    {"id": 1904, "title": "Femmes — Vêtements"},
    {"id": 1188, "title": "Femmes — Chaussures"},
    {"id": 4,    "title": "Hommes — Vêtements"},
    {"id": 1189, "title": "Hommes — Chaussures"},
    {"id": 1231, "title": "Enfants"},
    {"id": 7,    "title": "Bijoux & Accessoires"},
    {"id": 2,    "title": "Électronique"},
    {"id": 4172, "title": "Maison"},
    {"id": 42,   "title": "Loisirs"},
    {"id": 1615, "title": "Beauté & Bien-être"},
]

_FALLBACK_SIZES = [
    {"id": 1,   "title": "XXS"},
    {"id": 2,   "title": "XS"},
    {"id": 3,   "title": "S"},
    {"id": 4,   "title": "M"},
    {"id": 5,   "title": "L"},
    {"id": 6,   "title": "XL"},
    {"id": 7,   "title": "XXL"},
    {"id": 8,   "title": "XXXL"},
    {"id": 102, "title": "36"},
    {"id": 103, "title": "38"},
    {"id": 104, "title": "40"},
    {"id": 105, "title": "42"},
    {"id": 106, "title": "44"},
    {"id": 107, "title": "46"},
    {"id": 174, "title": "36 (EU) / 3.5 (UK)"},
    {"id": 175, "title": "37 (EU) / 4 (UK)"},
    {"id": 176, "title": "38 (EU) / 5 (UK)"},
    {"id": 177, "title": "39 (EU) / 6 (UK)"},
    {"id": 178, "title": "40 (EU) / 6.5 (UK)"},
    {"id": 179, "title": "41 (EU) / 7.5 (UK)"},
    {"id": 180, "title": "42 (EU) / 8 (UK)"},
    {"id": 181, "title": "43 (EU) / 9 (UK)"},
    {"id": 182, "title": "44 (EU) / 9.5 (UK)"},
    {"id": 183, "title": "45 (EU) / 10.5 (UK)"},
    {"id": 184, "title": "46 (EU) / 11 (UK)"},
]
