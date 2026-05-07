"""
Vinted API client — session cookie, anti-ban, cache suggestions.

Corrections vs version précédente :
  - Session dédiée aux suggestions (pre-warmée au démarrage)
  - Timeout courts pour l'autocomplete (5s max)
  - prewarm() à appeler dans on_ready()
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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

_COOKIE_TTL        = 3600  # secondes
_RATE_LIMIT_WAIT   = 60
_SUGGEST_CACHE_TTL = 300


class _DomainState:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.cookie_ts: float = 0.0
        self.lock = asyncio.Lock()
        self.backoff_until: float = 0.0


class VintedClient:
    def __init__(self):
        self._domains: dict[str, _DomainState] = {}
        # Session séparée, pre-warmée, pour les suggestions (autocomplete / View)
        self._sg_session: Optional[aiohttp.ClientSession] = None
        self._sg_ts: float = 0.0
        self._sg_lock = asyncio.Semaphore(3)
        self._cache: dict[str, tuple[float, list]] = {}

    # ── Démarrage ─────────────────────────────────────────────────────────────

    async def prewarm(self, domain: str = "www.vinted.fr"):
        """
        Initialise la session de suggestion au démarrage du bot.
        Doit être appelé dans on_ready() pour éviter les timeouts au premier usage.
        """
        await self._ensure_sg_session(domain)
        logger.info(f"Session suggestion pré-initialisée pour {domain}")

    # ── Recherche d'articles (monitoring) ─────────────────────────────────────

    async def search(self, domain: str, params: dict) -> Optional[list[dict]]:
        state = self._get_state(domain)
        async with state.lock:
            rem = state.backoff_until - time.monotonic()
            if rem > 0:
                logger.info(f"[{domain}] Backoff {rem:.0f}s")
                await asyncio.sleep(rem)
            await self._ensure_main_session(domain, state)
            return await self._do_search(domain, state, params)

    # ── Suggestions (UI guidée) ────────────────────────────────────────────────

    async def search_brands(self, query: str, domain: str = "www.vinted.fr") -> list[dict]:
        if len(query) < 2:
            return []
        key = f"brands:{domain}:{query.lower()}"
        hit = self._cache_get(key)
        if hit is not None:
            return hit

        # Vinted a changé le nom du paramètre selon les versions — on essaie les deux
        for param_name in ("search_text", "q"):
            data = await self._sg_get(
                domain,
                f"https://{domain}/api/v2/brands",
                {param_name: query, "per_page": 25},
            )
            if data is None:
                continue

            # Le format de réponse peut varier : {"brands": [...]} ou liste directe
            raw = data.get("brands") if isinstance(data, dict) else data
            if not isinstance(raw, list):
                continue

            result = [
                {"id": b["id"], "title": b["title"]}
                for b in raw
                if b.get("id") and b.get("title")
            ]
            if result:
                self._cache_set(key, result)
                return result

        # Fallback : filtrer la liste statique de marques populaires
        q = query.lower()
        fallback = [b for b in _POPULAR_BRANDS if q in b["title"].lower()]
        logger.warning(f"[brands] API échouée pour '{query}' — fallback {len(fallback)} résultats")
        return fallback

    async def get_catalogs(self, domain: str = "www.vinted.fr") -> list[dict]:
        key = f"catalogs:{domain}"
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        data = await self._sg_get(domain, f"https://{domain}/api/v2/catalog",
                                  {"per_page": 100})
        cats: list[dict] = []
        for c in (data or {}).get("catalogs", []):
            if c.get("id") and c.get("title"):
                cats.append({"id": c["id"], "title": c["title"]})
                for sub in c.get("catalogs", []):
                    if sub.get("id") and sub.get("title"):
                        cats.append({"id": sub["id"], "title": f"  └ {sub['title']}"})
        result = cats or _FALLBACK_CATALOGS
        self._cache_set(key, result)
        return result

    async def get_sizes(self, domain: str = "www.vinted.fr",
                        catalog_id: Optional[int] = None) -> list[dict]:
        key = f"sizes:{domain}:{catalog_id}"
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        params: dict = {"per_page": 200}
        if catalog_id:
            params["catalog_id"] = catalog_id
        data = await self._sg_get(domain, f"https://{domain}/api/v2/sizes", params)
        sizes = [{"id": s["id"], "title": s.get("title", s.get("name", ""))}
                 for s in (data or {}).get("sizes", []) if s.get("id")]
        result = sizes or _FALLBACK_SIZES
        self._cache_set(key, result)
        return result

    # ── Fermeture ─────────────────────────────────────────────────────────────

    async def close(self):
        for st in self._domains.values():
            if st.session:
                await st.session.close()
        if self._sg_session:
            await self._sg_session.close()
        self._domains.clear()

    # ── Privé ──────────────────────────────────────────────────────────────────

    def _get_state(self, domain: str) -> _DomainState:
        if domain not in self._domains:
            self._domains[domain] = _DomainState()
        return self._domains[domain]

    def _headers(self) -> dict:
        return {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    async def _ensure_main_session(self, domain: str, state: _DomainState):
        now = time.monotonic()
        if state.session and (now - state.cookie_ts) < _COOKIE_TTL:
            return
        if state.session:
            await state.session.close()
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=True, limit=4),
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            headers={"User-Agent": random.choice(_USER_AGENTS)},
        )
        try:
            async with session.get(f"https://{domain}", headers=self._headers(),
                                   allow_redirects=True,
                                   timeout=aiohttp.ClientTimeout(total=15)) as r:
                logger.info(f"[{domain}] Session principale initialisée ({r.status})")
        except Exception as e:
            logger.warning(f"[{domain}] Init session principale : {e}")
        state.session = session
        state.cookie_ts = now

    async def _ensure_sg_session(self, domain: str = "www.vinted.fr"):
        now = time.monotonic()
        if self._sg_session and (now - self._sg_ts) < _COOKIE_TTL:
            return
        if self._sg_session:
            await self._sg_session.close()
        # unsafe=True : accepte les cookies sans domaine strict (nécessaire pour Vinted)
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=True, limit=8),
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            headers={"User-Agent": random.choice(_USER_AGENTS)},
        )
        try:
            async with session.get(
                f"https://{domain}",
                headers=self._headers(),
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                logger.info(f"Session suggestion initialisée ({r.status}), "
                            f"cookies: {list(session.cookie_jar)!r:.120}")
        except Exception as e:
            logger.warning(f"Init session suggestion : {e}")
        self._sg_session = session
        self._sg_ts = now

    async def _sg_get(self, domain: str, url: str, params: dict) -> Optional[dict]:
        """GET pour l'UI interactive — timeout 10s, logs d'erreur visibles."""
        if not self._sg_session or (time.monotonic() - self._sg_ts) >= _COOKIE_TTL:
            await self._ensure_sg_session(domain)
        async with self._sg_lock:
            try:
                async with self._sg_session.get(
                    url,
                    params=params,
                    headers={
                        **self._headers(),
                        "Referer": f"https://{domain}/",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 200:
                        return await r.json(content_type=None)
                    # Log WARNING (visible dans docker logs) pour diagnostiquer
                    body = await r.text()
                    logger.warning(f"[sg] {url} → HTTP {r.status} | {body[:200]}")
            except asyncio.TimeoutError:
                logger.warning(f"[sg] Timeout sur {url}")
            except Exception as e:
                logger.warning(f"[sg] Erreur {url} : {e}")
        return None

    async def _do_search(self, domain: str, state: _DomainState,
                         user_params: dict) -> Optional[list[dict]]:
        api_params = {"order": "newest_first", "per_page": 20, "page": 1}
        for k, v in user_params.items():
            if v is not None and v != "" and v != []:
                api_params[k] = v
        url = f"https://{domain}/api/v2/catalog/items"
        try:
            async with state.session.get(
                url, params=api_params,
                headers={**self._headers(), "Referer": f"https://{domain}/catalog",
                         "X-Requested-With": "XMLHttpRequest"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    items = data.get("items", [])
                    logger.debug(f"[{domain}] {len(items)} articles")
                    return items
                if r.status == 429:
                    wait = max(int(r.headers.get("Retry-After", _RATE_LIMIT_WAIT)), _RATE_LIMIT_WAIT)
                    logger.warning(f"[{domain}] 429 — backoff {wait}s")
                    state.backoff_until = time.monotonic() + wait
                    return None
                if r.status in (401, 403):
                    state.cookie_ts = 0.0
                    return None
                if r.status >= 500:
                    state.backoff_until = time.monotonic() + 30
                    return None
                return None
        except asyncio.TimeoutError:
            logger.warning(f"[{domain}] Timeout")
            return None
        except Exception as e:
            logger.error(f"[{domain}] {e}")
            return None

    def _cache_get(self, key: str) -> Optional[list]:
        e = self._cache.get(key)
        if e and (time.monotonic() - e[0]) < _SUGGEST_CACHE_TTL:
            return e[1]
        return None

    def _cache_set(self, key: str, val: list):
        self._cache[key] = (time.monotonic(), val)


# ── Fallbacks & données statiques ────────────────────────────────────────────

# Marques populaires — utilisées si l'API Vinted brands ne répond pas
_POPULAR_BRANDS = [
    {"id": 53,   "title": "Nike"},
    {"id": 12,   "title": "Adidas"},
    {"id": 308,  "title": "Zara"},
    {"id": 536,  "title": "H&M"},
    {"id": 304,  "title": "Levi's"},
    {"id": 3,    "title": "Ralph Lauren"},
    {"id": 213,  "title": "Tommy Hilfiger"},
    {"id": 362,  "title": "The North Face"},
    {"id": 19,   "title": "Lacoste"},
    {"id": 376,  "title": "Stone Island"},
    {"id": 52,   "title": "New Balance"},
    {"id": 77,   "title": "Puma"},
    {"id": 280,  "title": "Vans"},
    {"id": 161,  "title": "Converse"},
    {"id": 586,  "title": "Jordan"},
    {"id": 473,  "title": "Supreme"},
    {"id": 316,  "title": "Mango"},
    {"id": 439,  "title": "Pull&Bear"},
    {"id": 16,   "title": "Bershka"},
    {"id": 441,  "title": "Stradivarius"},
    {"id": 362,  "title": "Hollister"},
    {"id": 215,  "title": "Calvin Klein"},
    {"id": 148,  "title": "Hugo Boss"},
    {"id": 267,  "title": "Guess"},
    {"id": 371,  "title": "Lululemon"},
    {"id": 64,   "title": "Under Armour"},
    {"id": 258,  "title": "Patagonia"},
    {"id": 88,   "title": "Columbia"},
    {"id": 374,  "title": "Timberland"},
    {"id": 195,  "title": "Dr. Martens"},
    {"id": 9,    "title": "Gucci"},
    {"id": 7,    "title": "Louis Vuitton"},
    {"id": 10,   "title": "Chanel"},
    {"id": 11,   "title": "Dior"},
    {"id": 237,  "title": "Balenciaga"},
    {"id": 468,  "title": "Off-White"},
    {"id": 2,    "title": "Burberry"},
    {"id": 5,    "title": "Prada"},
    {"id": 6,    "title": "Versace"},
    {"id": 4,    "title": "Armani"},
    {"id": 17,   "title": "Massimo Dutti"},
    {"id": 338,  "title": "Sandro"},
    {"id": 424,  "title": "Maje"},
    {"id": 443,  "title": "Ba&sh"},
    {"id": 178,  "title": "A.P.C."},
    {"id": 101,  "title": "Kenzo"},
    {"id": 103,  "title": "Isabel Marant"},
]

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
    {"id": 206, "title": "XXS / 32"},
    {"id": 207, "title": "XS / 34"},
    {"id": 208, "title": "S / 36"},
    {"id": 209, "title": "M / 38"},
    {"id": 210, "title": "L / 40"},
    {"id": 211, "title": "XL / 42"},
    {"id": 212, "title": "XXL / 44"},
    {"id": 213, "title": "XXXL / 46"},
    {"id": 214, "title": "4XL / 48+"},
    {"id": 174, "title": "Chaussures 36"},
    {"id": 175, "title": "Chaussures 37"},
    {"id": 176, "title": "Chaussures 38"},
    {"id": 177, "title": "Chaussures 39"},
    {"id": 178, "title": "Chaussures 40"},
    {"id": 179, "title": "Chaussures 41"},
    {"id": 180, "title": "Chaussures 42"},
    {"id": 181, "title": "Chaussures 43"},
    {"id": 182, "title": "Chaussures 44"},
    {"id": 183, "title": "Chaussures 45"},
    {"id": 184, "title": "Chaussures 46"},
    {"id": 215, "title": "Taille unique"},
]
