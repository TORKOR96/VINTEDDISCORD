"""
Moniteur de fond — un asyncio.Task par filtre, avec polling espacé et jitter.

Stratégie anti-spam au démarrage :
  - Premier sondage : on marque tous les articles "vus" SANS envoyer de notif.
  - Sondages suivants : on envoie uniquement les nouveaux articles.

Stratégie anti-ban :
  - Chaque filtre a sa propre boucle avec délai configurable + jitter ±20 %.
  - Les filtres d'un même domaine sont décalés dans le temps (pas de salve).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.bot import VintedBot

logger = logging.getLogger(__name__)


class Monitor:
    def __init__(self, bot: "VintedBot"):
        self.bot = bot
        self._tasks: dict[int, asyncio.Task] = {}
        self._running = False

    async def start(self):
        self._running = True
        asyncio.create_task(self._manager_loop(), name="monitor-manager")
        logger.info("Monitor démarré")

    async def stop(self):
        self._running = False
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    # ── Boucle de gestion des tâches ──────────────────────────────────────────

    async def _manager_loop(self):
        """
        Vérifie chaque minute si des filtres ont été ajoutés/supprimés
        et synchronise les tâches asyncio en conséquence.
        """
        while self._running:
            try:
                filters = await self.bot.db.get_filters()
                active_ids = {f["id"] for f in filters}

                # Annuler les tâches des filtres supprimés
                for fid in list(self._tasks):
                    if fid not in active_ids or self._tasks[fid].done():
                        self._tasks[fid].cancel()
                        del self._tasks[fid]

                # Démarrer les tâches pour les nouveaux filtres
                # Les filtres sont décalés pour éviter les salves sur un même domaine
                pending = [f for f in filters if f["id"] not in self._tasks]
                for idx, f in enumerate(pending):
                    delay = idx * _stagger_delay(self.bot.config.POLL_INTERVAL, len(pending))
                    fid = f["id"]
                    self._tasks[fid] = asyncio.create_task(
                        self._filter_loop(dict(f), delay),
                        name=f"filter-{fid}",
                    )

            except Exception as exc:
                logger.exception(f"Erreur dans le manager : {exc}")

            await asyncio.sleep(60)

    # ── Boucle d'un filtre ────────────────────────────────────────────────────

    async def _filter_loop(self, filter_data: dict, initial_delay: float):
        filter_id = filter_data["id"]
        name = filter_data.get("name", f"#{filter_id}")

        if initial_delay > 0:
            logger.debug(f"[{name}] Décalage initial : {initial_delay:.1f}s")
            await asyncio.sleep(initial_delay)

        while self._running:
            try:
                # Recharger le filtre (peut avoir été modifié)
                fresh = await self.bot.db.get_filter(filter_id)
                if fresh is None:
                    logger.info(f"[{name}] Filtre supprimé — arrêt de la boucle")
                    return
                filter_data = dict(fresh)
                await self._poll(filter_data)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception(f"[{name}] Erreur lors du sondage : {exc}")

            interval = self.bot.config.POLL_INTERVAL
            jitter = random.uniform(-interval * 0.20, interval * 0.20)
            wait = max(60, interval + jitter)
            logger.debug(f"[{name}] Prochain sondage dans {wait:.0f}s")
            await asyncio.sleep(wait)

    # ── Sondage effectif ──────────────────────────────────────────────────────

    async def _poll(self, filter_data: dict):
        filter_id = filter_data["id"]
        name = filter_data.get("name", f"#{filter_id}")
        domain = filter_data.get("domain") or "www.vinted.fr"

        params = _build_params(filter_data)
        items = await self.bot.vinted.search(domain, params)
        if items is None:
            return  # Erreur réseau ou rate-limit — on réessaiera au prochain cycle

        is_first = not await self.bot.db.has_any_seen(filter_id)

        if is_first:
            # Premier sondage : initialiser la base sans envoyer de notif
            ids = [str(item["id"]) for item in items if item.get("id")]
            await self.bot.db.mark_seen_bulk(ids, filter_id)
            logger.info(f"[{name}] Initialisation : {len(ids)} articles marqués comme vus")
            return

        # Sondages suivants : notifier uniquement les nouveaux
        new_items: list[dict] = []
        for item in items:
            iid = str(item.get("id", ""))
            if iid and not await self.bot.db.is_seen(iid, filter_id):
                new_items.append(item)
                await self.bot.db.mark_seen(iid, filter_id)

        if not new_items:
            logger.debug(f"[{name}] Aucun nouvel article")
            return

        logger.info(f"[{name}] {len(new_items)} nouvel(s) article(s) trouvé(s)")
        await self._send_notifications(new_items, filter_data)

    # ── Envoi Discord ─────────────────────────────────────────────────────────

    async def _send_notifications(self, items: list[dict], filter_data: dict):
        channel_id = int(filter_data["channel_id"])
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            logger.warning(f"Salon {channel_id} introuvable pour le filtre #{filter_data['id']}")
            return

        # On envoie du plus ancien au plus récent (API renvoie newest_first)
        for item in reversed(items):
            embed = self.bot.embeds.build_item_embed(item, filter_data)
            try:
                await channel.send(embed=embed)
                await asyncio.sleep(0.5)  # évite les floods Discord
            except Exception as exc:
                logger.error(f"Impossible d'envoyer l'embed dans {channel_id} : {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _stagger_delay(poll_interval: int, n_filters: int) -> float:
    """Espacement entre les filtres pour éviter de les déclencher tous en même temps."""
    if n_filters <= 1:
        return 0.0
    return poll_interval / n_filters


def _build_params(f: dict) -> dict:
    """Construit le dict de paramètres pour l'API Vinted à partir d'un filtre."""
    params: dict = {}

    # Mots-clés de base
    search_parts: list[str] = []
    if f.get("search_text"):
        search_parts.append(f["search_text"])

    # Marque : si on a un vrai ID Vinted → paramètre brand_ids[] (filtrage exact)
    # Sinon si on a juste le nom (fallback) → on l'ajoute au search_text
    if f.get("brand_ids"):
        params["brand_ids[]"] = f["brand_ids"].split(",")
    elif f.get("brand_titles"):
        # Même comportement que la barre de recherche Vinted
        search_parts.append(f["brand_titles"])

    if search_parts:
        params["search_text"] = " ".join(search_parts)

    if f.get("min_price") is not None:
        params["price_from"] = f["min_price"]
    if f.get("max_price") is not None:
        params["price_to"] = f["max_price"]
    if f.get("size_ids"):
        params["size_ids[]"] = f["size_ids"].split(",")
    if f.get("catalog_ids"):
        params["catalog_ids[]"] = f["catalog_ids"].split(",")
    if f.get("condition_ids"):
        params["status_ids[]"] = f["condition_ids"].split(",")

    return params
