"""
Bot Discord principal — slash commands + démarrage du moniteur.
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

# Domaines Vinted disponibles (nom affiché → domaine)
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


class VintedBot(commands.Bot):

    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.config = Config()
        self.db = Database(self.config.DATABASE_PATH)
        self.vinted = VintedClient()
        self.embeds = EmbedBuilder()
        self.monitor = Monitor(self)

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    async def setup_hook(self):
        await self.db.connect()
        _register_commands(self)
        await self.tree.sync()
        logger.info("Slash commands synchronisées")

    async def on_ready(self):
        logger.info(f"Connecté en tant que {self.user} (ID={self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Vinted 🛍️",
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
            logger.info("Nettoyage quotidien des articles vus terminé")


# ── Slash commands ────────────────────────────────────────────────────────────

def _register_commands(bot: VintedBot):

    # ── /ajouter-filtre ───────────────────────────────────────────────────────
    @bot.tree.command(
        name="ajouter-filtre",
        description="Créer une nouvelle surveillance Vinted dans un salon",
    )
    @app_commands.describe(
        nom="Nom du filtre (ex: Nike Air Max taille 42)",
        salon="Salon Discord qui recevra les notifications",
        recherche="Mots-clés de recherche (optionnel)",
        prix_min="Prix minimum en € (optionnel)",
        prix_max="Prix maximum en € (optionnel)",
        pays="Pays Vinted à surveiller (défaut : France)",
    )
    @app_commands.choices(pays=[
        app_commands.Choice(name=label, value=domain)
        for label, domain in VINTED_DOMAINS.items()
    ])
    @app_commands.checks.has_permissions(manage_channels=True)
    async def cmd_add_filter(
        interaction: discord.Interaction,
        nom: str,
        salon: discord.TextChannel,
        recherche: str = "",
        prix_min: float = None,
        prix_max: float = None,
        pays: str = "www.vinted.fr",
    ):
        await interaction.response.defer(ephemeral=True)

        filter_id = await bot.db.add_filter(
            guild_id=str(interaction.guild_id),
            channel_id=str(salon.id),
            name=nom,
            search_text=recherche,
            min_price=prix_min,
            max_price=prix_max,
            domain=pays,
        )

        lines = [f"✅ Filtre **{nom}** (ID `#{filter_id}`) créé !"]
        lines.append(f"Les nouvelles annonces seront envoyées dans {salon.mention}.")
        if recherche:
            lines.append(f"🔍 Recherche : `{recherche}`")
        if prix_min is not None or prix_max is not None:
            lo = f"{prix_min}€" if prix_min is not None else "0€"
            hi = f"{prix_max}€" if prix_max is not None else "∞"
            lines.append(f"💰 Prix : {lo} – {hi}")
        lines.append(f"🌐 Domaine : `{pays}`")
        lines.append(f"\n⏱️ Premier sondage dans ~{bot.config.POLL_INTERVAL}s — les articles existants ne déclencheront **pas** de notification.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

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
        description="Supprimer un filtre (utilisez /liste-filtres pour connaître l'ID)",
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
            f"(aperçu des 3 premiers) :",
            ephemeral=True,
        )
        for item in items[:3]:
            embed = bot.embeds.build_item_embed(item, filter_data)
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /aide ─────────────────────────────────────────────────────────────────
    @bot.tree.command(name="aide", description="Afficher l'aide du bot Vinted")
    async def cmd_help(interaction: discord.Interaction):
        embed = bot.embeds.build_help_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Gestion des erreurs de permission ─────────────────────────────────────
    @cmd_add_filter.error
    @cmd_delete_filter.error
    async def _permission_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ Tu as besoin de la permission **Gérer les salons** pour cette commande.",
                ephemeral=True,
            )
        else:
            logger.exception(f"Erreur slash command : {error}")
            await interaction.response.send_message(
                "❌ Une erreur inattendue s'est produite.", ephemeral=True
            )
