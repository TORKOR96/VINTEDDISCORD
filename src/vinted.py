"""
Vinted API client — accès non authentifié via session cookie.

Stratégie anti-ban :
  - Rotation des User-Agents
  - Refresh automatique des cookies toutes les heures
  - Backoff exponentiel sur 429 / 5xx
  - Timeout strict par requête
  - Pas de parallélisme : une requête à la fois par domaine
"""

import asyncio
import logging
import random
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Pool de User-Agents à jour (Chrome/Firefox récents)
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

# TTL du cookie de session (secondes)
_COOKIE_TTL = 3600

# Délai minimum après un 429 (secondes)
_RATE_LIMIT_BACKOFF = 60


class _DomainState:
    """État par domaine Vinted : session aiohttp + timestamp du dernier cookie."""

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.cookie_ts: float = 0.0
        self.lock = asyncio.Lock()  # une seule requête à la fois par domaine
        self.backoff_until: float = 0.0  # ne pas requêter avant ce timestamp

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None


class VintedClient:
    def __init__(self):
        self._domains: dict[str, _DomainState] = {}

    # ── Public ─────────────────────────────────────────────────────────────────

    async def search(self, domain: str, params: dict) -> Optional[list[dict]]:
        """
        Recherche des articles sur le domaine Vinted donné.
        Retourne la liste d'articles, ou None en cas d'erreur non fatale.
        """
        state = self._get_state(domain)

        async with state.lock:
            # Respecter le backoff si rate-limité
            remaining = state.backoff_until - time.monotonic()
            if remaining > 0:
                logger.info(f"[{domain}] Backoff actif, attente {remaining:.0f}s")
                await asyncio.sleep(remaining)

            # Rafraîchir la session/cookie si nécessaire
            await self._ensure_session(domain, state)

            return await self._do_search(domain, state, params)

    async def close(self):
        for state in self._domains.values():
            await state.close()
        self._domains.clear()

    # ── Privé ──────────────────────────────────────────────────────────────────

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
            return  # cookie encore valide

        # Fermer l'ancienne session
        if state.session:
            await state.session.close()
            state.session = None

        ua = random.choice(_USER_AGENTS)
        connector = aiohttp.TCPConnector(ssl=True, limit=4)
        session = aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.CookieJar(),
            headers={"User-Agent": ua},
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
            logger.warning(f"[{domain}] Impossible d'initialiser la session : {exc}")

        state.session = session
        state.cookie_ts = now

    async def _do_search(
        self, domain: str, state: _DomainState, user_params: dict
    ) -> Optional[list[dict]]:
        api_params: dict = {
            "order": "newest_first",
            "per_page": 20,
            "page": 1,
        }
        # Ajouter les paramètres utilisateur non vides
        for key, val in user_params.items():
            if val is not None and val != "" and val != []:
                api_params[key] = val

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
                status = resp.status

                if status == 200:
                    data = await resp.json(content_type=None)
                    items = data.get("items", [])
                    logger.debug(f"[{domain}] {len(items)} articles récupérés")
                    return items

                if status == 429:
                    retry_after = int(resp.headers.get("Retry-After", _RATE_LIMIT_BACKOFF))
                    wait = max(retry_after, _RATE_LIMIT_BACKOFF)
                    logger.warning(f"[{domain}] 429 — attente {wait}s")
                    state.backoff_until = time.monotonic() + wait
                    return None

                if status in (401, 403):
                    logger.warning(f"[{domain}] {status} — on force le refresh des cookies")
                    state.cookie_ts = 0.0  # force refresh au prochain appel
                    return None

                if status >= 500:
                    logger.warning(f"[{domain}] Erreur serveur {status}")
                    state.backoff_until = time.monotonic() + 30
                    return None

                logger.warning(f"[{domain}] Réponse inattendue : {status}")
                return None

        except asyncio.TimeoutError:
            logger.warning(f"[{domain}] Timeout sur la requête API")
            return None
        except aiohttp.ClientError as exc:
            logger.error(f"[{domain}] Erreur réseau : {exc}")
            return None
        except Exception as exc:
            logger.error(f"[{domain}] Erreur inattendue : {exc}")
            return None
