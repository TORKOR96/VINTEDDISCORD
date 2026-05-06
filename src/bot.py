"""
Bot Discord principal — slash commands avec autocomplete guidé comme Vinted.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.config import Config
from src.database import Database
from src.embeds import EmbedBuilder
from src.monitor import Monitor, _build_params
from src.vinted import VintedClient

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────

VINTED_DOMAINS: dict[str, str] = {
    "France":       "www.vinted.fr",
    "Belgique":     "www.vinted.be",
    "Luxembourg":   "www.vinted.lu",
    "Allemagne":    "www.vinted.de",
    "Pays-Bas":     "www.vinted.nl",
    "Espagne":      "www.vinted.es",
    "Italie":       "www.vinted.it",
    "Pologne":      "www.vinted.pl",
    "Royaume-Uni":  "www.vinted.co.uk",
    "Autriche":     "www.vinted.at",
    "Portugal":     "www.vinted.pt",
    "Hongrie":      "www.vinted.hu",
    "Roumanie":     "www.vinted.ro",
    "Tchéquie":     "www.vinted.cz",
    "Slovaquie":    "www.vinted.sk",
}

# États des articles (IDs Vinted stables)
CONDITIONS = [
    app_commands.Choice(name="🏷️  Neuf avec étiquettes",   value="6"),
    app_commands.Choice(name="✨ Neuf sans étiquettes",  value="1"),
    app_commands.Choice(name="👍 Très bon état",         value="2"),
    app_commands.Choice(name="👌 Bon état",              value="3"),
    app_commands.Choice(name="🙂 État satisfaisant",     value="4"),
]

CONDITION_LABELS = {
    "6": "Neuf avec étiquettes",
    "1": "Neuf sans étiquettes",
    "2": "Très bon état",
    "3": "Bon état",
    "4": "État satisfaisant",
}


def _domain_from_interaction(interaction: discord.Interaction) -> str:
    """Lit le paramètre `pays` déjà saisi dans l'interaction en cours."""
    try:
        pays = interaction.namespace.pays
        if pays:
            return pays
    except AttributeError:
        pass
    return "www.vinted.fr"


class VintedBot(commands.Bot):

    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = Config()
        self.db = Database(self.config.DATABASE_PATH)
        self.vinted = VintedClient()
        self.embeds = EmbedBuilder()
        self.monitor = Monitor(self)

    async def setup_hook(self):
        await self.db.connect()
        _register_commands(self)
        await self.tree.sync()
        logger.info("Slash commands synchronisées")

    async def on_ready(self):
        logger.info(f"Connecté : {self.user} (ID={self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="Vinted 🛍️"
            )
        )
        await self.monitor.start()
        import asyncio
        asyncio.create_task(self._daily_cleanup(), name="daily-cleanup")

    async def close(self):
        await self.monitor.stop()
        await self.vinted.close()
        await self.db.close()
        await super().close()

    async def _daily_cleanup(self):
        import asyncio
        while True:
            await asyncio.sleep(86_400)
            await self.db.cleanup_old_seen(self.config.SEEN_ITEMS_RETENTION_DAYS)
            logger.info("Nettoyage quotidien terminé")


# ── Commandes slash ────────────────────────────────────────────────────────────

def _register_commands(bot: VintedBot):

    # ── /ajouter-filtre ───────────────────────────────────────────────────────
    @bot.tree.command(
        name="ajouter-filtre",
        description="Créer une surveillance Vinted dans un salon Discord",
    )
    @app_commands.describe(
        nom="Nom du filtre  (ex: Nike Air Max 42)",
        salon="Salon qui recevra les notifications",
        pays="Pays Vinted à surveiller",
        recherche="Mots-clés libres (comme la barre de recherche Vinted)",
        marque="Marque — tapez au moins 2 lettres pour voir les suggestions",
        categorie="Catégorie — tapez pour filtrer",
        taille="Taille — tapez pour filtrer (ex: M, 42, XL…)",
        etat="État de l'article",
        prix_min="Prix minimum (€)",
        prix_max="Prix maximum (€)",
    )
    @app_commands.choices(pays=[
        app_commands.Choice(name=label, value=domain)
        for label, domain in VINTED_DOMAINS.items()
    ])
    @app_commands.choices(etat=CONDITIONS)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def cmd_add_filter(
        interaction: discord.Interaction,
        nom: str,
        salon: discord.TextChannel,
        pays: str = "www.vinted.fr",
        recherche: str = "",
        marque: str = "",
        categorie: str = "",
        taille: str = "",
        etat: str = "",
        prix_min: float = None,
        prix_max: float = None,
    ):
        await interaction.response.defer(ephemeral=True)

        # Décoder les valeurs "id|titre" venant de l'autocomplete
        brand_id, brand_title = _parse_choice(marque)
        cat_id,   cat_title   = _parse_choice(categorie)
        size_id,  size_title  = _parse_choice(taille)

        condition_label = CONDITION_LABELS.get(etat, etat)

        filter_id = await bot.db.add_filter(
            guild_id=str(interaction.guild_id),
            channel_id=str(salon.id),
            name=nom,
            search_text=recherche,
            min_price=prix_min,
            max_price=prix_max,
            brand_ids=brand_id,
            brand_titles=brand_title,
            size_ids=size_id,
            size_titles=size_title,
            catalog_ids=cat_id,
            catalog_titles=cat_title,
            condition_ids=etat,
            domain=pays,
        )

        lines = [f"✅ Filtre **{nom}** (ID `#{filter_id}`) créé !"]
        lines.append(f"Les nouvelles annonces seront envoyées dans {salon.mention}.")
        if recherche:
            lines.append(f"🔍 Recherche : `{recherche}`")
        if brand_title:
            lines.append(f"👟 Marque : **{brand_title}**")
        if cat_title:
            lines.append(f"🗂️  Catégorie : **{cat_title}**")
        if size_title:
            lines.append(f"📏 Taille : **{size_title}**")
        if condition_label:
            lines.append(f"🏷️  État : **{condition_label}**")
        if prix_min is not None or prix_max is not None:
            lo = f"{prix_min}€" if prix_min is not None else "0€"
            hi = f"{prix_max}€" if prix_max is not None else "∞"
            lines.append(f"💰 Prix : {lo} – {hi}")
        lines.append(f"🌐 Pays : `{pays}`")
        lines.append(
            f"\n⏱️ Premier sondage dans ~{bot.config.POLL_INTERVAL}s "
            "— les articles actuels ne déclencheront **pas** de notification."
        )

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ── Autocomplete : marque ─────────────────────────────────────────────────
    @cmd_add_filter.autocomplete("marque")
    async def autocomplete_brand(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if len(current) < 2:
            return [app_commands.Choice(name="Tapez au moins 2 lettres…", value="")]
        domain = _domain_from_interaction(interaction)
        brands = await bot.vinted.search_brands(current, domain)
        return [
            app_commands.Choice(
                name=b["title"][:100],
                value=f"{b['id']}|{b['title']}"[:100],
            )
            for b in brands[:25]
        ]

    # ── Autocomplete : catégorie ──────────────────────────────────────────────
    @cmd_add_filter.autocomplete("categorie")
    async def autocomplete_catalog(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        domain = _domain_from_interaction(interaction)
        catalogs = await bot.vinted.get_catalogs(domain)
        q = current.lower()
        matches = [
            c for c in catalogs
            if q in c["title"].lower()
        ] if q else catalogs
        return [
            app_commands.Choice(
                name=c["title"][:100],
                value=f"{c['id']}|{c['title']}"[:100],
            )
            for c in matches[:25]
        ]

    # ── Autocomplete : taille ─────────────────────────────────────────────────
    @cmd_add_filter.autocomplete("taille")
    async def autocomplete_size(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        domain = _domain_from_interaction(interaction)
        # Essayer d'inférer le catalog_id depuis le paramètre categorie déjà saisi
        cat_id = None
        try:
            raw_cat = interaction.namespace.categorie
            if raw_cat:
                cat_id_str, _ = raw_cat.split("|", 1)
                cat_id = int(cat_id_str)
        except (AttributeError, ValueError):
            pass

        sizes = await bot.vinted.get_sizes(current, cat_id, domain)
        return [
            app_commands.Choice(
                name=s["title"][:100],
                value=f"{s['id']}|{s['title']}"[:100],
            )
            for s in sizes[:25]
        ]

    # ── /liste-filtres ────────────────────────────────────────────────────────
    @bot.tree.command(
        name="liste-filtres",
        description="Afficher tous les filtres actifs sur ce serveur",
    )
    async def cmd_list_filters(interaction: discord.Interaction):
        rows = await bot.db.get_filters(str(interaction.guild_id))
        embed = bot.embeds.build_filter_list_embed(
            [dict(r) for r in rows], interaction.guild.name
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /supprimer-filtre ─────────────────────────────────────────────────────
    @bot.tree.command(
        name="supprimer-filtre",
        description="Supprimer un filtre (voir les IDs avec /liste-filtres)",
    )
    @app_commands.describe(id="ID du filtre à supprimer")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def cmd_delete_filter(interaction: discord.Interaction, id: int):
        row = await bot.db.get_filter(id)
        if row is None or str(row["guild_id"]) != str(interaction.guild_id):
            await interaction.response.send_message(
                "❌ Filtre introuvable sur ce serveur.", ephemeral=True
            )
            return
        await bot.db.delete_filter(id, str(interaction.guild_id))
        await interaction.response.send_message(
            f"✅ Filtre **{row['name']}** (ID `#{id}`) supprimé.", ephemeral=True
        )

    # ── /tester-filtre ────────────────────────────────────────────────────────
    @bot.tree.command(
        name="tester-filtre",
        description="Tester un filtre et afficher les 3 premiers résultats (sans marquer comme vus)",
    )
    @app_commands.describe(id="ID du filtre à tester")
    async def cmd_test_filter(interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)
        row = await bot.db.get_filter(id)
        if row is None or str(row["guild_id"]) != str(interaction.guild_id):
            await interaction.followup.send("❌ Filtre introuvable sur ce serveur.", ephemeral=True)
            return

        filter_data = dict(row)
        params = _build_params(filter_data)
        items = await bot.vinted.search(filter_data.get("domain", "www.vinted.fr"), params)

        if not items:
            await interaction.followup.send(
                "⚠️ Aucun résultat ou erreur de connexion à Vinted.", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ **{len(items)} articles** trouvés pour le filtre **{filter_data['name']}** "
            f"— aperçu des 3 premiers :",
            ephemeral=True,
        )
        for item in items[:3]:
            embed = bot.embeds.build_item_embed(item, filter_data)
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /aide ─────────────────────────────────────────────────────────────────
    @bot.tree.command(name="aide", description="Afficher l'aide du bot Vinted")
    async def cmd_help(interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=bot.embeds.build_help_embed(), ephemeral=True
        )

    # ── Gestion des erreurs ───────────────────────────────────────────────────
    @cmd_add_filter.error
    @cmd_delete_filter.error
    async def _perm_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ Tu as besoin de la permission **Gérer les salons** pour cette commande.",
                ephemeral=True,
            )
        else:
            logger.exception(f"Erreur slash command : {error}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Une erreur inattendue s'est produite.", ephemeral=True
                )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_choice(raw: str) -> tuple[str, str]:
    """
    Décode une valeur autocomplete au format "id|titre".
    Retourne ("", "") si vide, ou ("id", "titre") sinon.
    Si pas de séparateur (saisie libre), retourne ("", raw).
    """
    if not raw:
        return "", ""
    if "|" in raw:
        parts = raw.split("|", 1)
        return parts[0].strip(), parts[1].strip()
    return "", raw.strip()
