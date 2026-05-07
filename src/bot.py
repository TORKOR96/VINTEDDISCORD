"""
Bot Discord — UI guidée avec menus interactifs, multi-sélection, modaux.

Flux /ajouter-filtre :
  1. Commande slash (nom + salon obligatoires)
  2. Embed de configuration + composants interactifs :
     - Select Pays          (1 choix)
     - Select État          (0-5 choix simultanés)
     - Select Taille        (0-N choix simultanés)
     - Bouton Marque        → Modal recherche → Select résultats
     - Bouton Mots-clés     → Modal (recherche libre + prix min/max)
     - Bouton Catégorie     → Select catégories Vinted
     - Bouton Créer ✅      → sauvegarde + confirme
"""

from __future__ import annotations

import logging
from typing import Optional

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
    "🇫🇷 France":       "www.vinted.fr",
    "🇧🇪 Belgique":     "www.vinted.be",
    "🇱🇺 Luxembourg":   "www.vinted.lu",
    "🇩🇪 Allemagne":    "www.vinted.de",
    "🇳🇱 Pays-Bas":     "www.vinted.nl",
    "🇪🇸 Espagne":      "www.vinted.es",
    "🇮🇹 Italie":       "www.vinted.it",
    "🇵🇱 Pologne":      "www.vinted.pl",
    "🇬🇧 Royaume-Uni":  "www.vinted.co.uk",
    "🇦🇹 Autriche":     "www.vinted.at",
    "🇵🇹 Portugal":     "www.vinted.pt",
    "🇭🇺 Hongrie":      "www.vinted.hu",
    "🇷🇴 Roumanie":     "www.vinted.ro",
    "🇨🇿 Tchéquie":     "www.vinted.cz",
    "🇸🇰 Slovaquie":    "www.vinted.sk",
}

# (label affiché, id API Vinted)
CONDITIONS: list[tuple[str, str]] = [
    ("🏷️ Neuf avec étiquettes",   "6"),
    ("✨ Neuf sans étiquettes",  "1"),
    ("👍 Très bon état",         "2"),
    ("👌 Bon état",              "3"),
    ("🙂 État satisfaisant",     "4"),
]
CONDITION_LABELS: dict[str, str] = {cid: label for label, cid in CONDITIONS}

_VINTED_GREEN = discord.Color.from_rgb(9, 182, 109)


# ── État du filtre en cours de configuration ──────────────────────────────────

class FilterState:
    def __init__(self, bot: "VintedBot", guild_id: int, channel_id: int, nom: str):
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.nom = nom
        self.message: Optional[discord.Message] = None

        # Critères
        self.domain = "www.vinted.fr"
        self.search_text = ""
        self.prix_min: Optional[float] = None
        self.prix_max: Optional[float] = None
        self.brand_id = ""
        self.brand_title = ""
        self.cat_id = ""
        self.cat_title = ""
        self.condition_ids: list[str] = []
        self.size_ids: list[str] = []
        self.size_titles: list[str] = []

    # ── Affichage ──────────────────────────────────────────────────────────────

    def summary_embed(self, title_prefix: str = "⚙️") -> discord.Embed:
        embed = discord.Embed(
            title=f"{title_prefix} **{self.nom}**",
            description=f"Salon de destination : <#{self.channel_id}>",
            color=_VINTED_GREEN,
        )
        domain_label = next(
            (lbl for lbl, d in VINTED_DOMAINS.items() if d == self.domain),
            self.domain,
        )
        embed.add_field(name="🌐 Pays",   value=domain_label,             inline=True)
        embed.add_field(name="🏷️ État(s)",
                        value=", ".join(CONDITION_LABELS[c] for c in self.condition_ids)
                              or "*(tous)*",
                        inline=True)
        embed.add_field(name="👟 Marque", value=self.brand_title or "*(toutes)*", inline=True)
        embed.add_field(name="📂 Catégorie",
                        value=self.cat_title.strip() or "*(toutes)*",
                        inline=True)
        embed.add_field(name="📏 Taille(s)",
                        value=", ".join(self.size_titles) or "*(toutes)*",
                        inline=True)
        search_val = f"`{self.search_text}`" if self.search_text else "*(aucun)*"
        embed.add_field(name="🔍 Mots-clés", value=search_val, inline=True)
        if self.prix_min is not None or self.prix_max is not None:
            lo = f"{self.prix_min}€" if self.prix_min is not None else "0€"
            hi = f"{self.prix_max}€" if self.prix_max is not None else "∞"
            embed.add_field(name="💰 Prix", value=f"{lo} – {hi}", inline=True)
        embed.set_footer(text="Modifiez les critères puis cliquez sur ✅ Créer le filtre")
        return embed


# ── Vue principale ────────────────────────────────────────────────────────────

class FilterSetupView(discord.ui.View):
    def __init__(self, state: FilterState, sizes: list[dict]):
        super().__init__(timeout=300)
        self.state = state
        self.add_item(_PaysSelect(state, self))
        self.add_item(_EtatSelect(state, self))
        self.add_item(_TailleSelect(state, self, sizes))

    # Édition du message stocké (utilisée par les modaux)
    async def refresh_message(self):
        if self.state.message:
            await self.state.message.edit(
                embed=self.state.summary_embed(), view=self
            )

    @discord.ui.button(label="👟 Marque",          style=discord.ButtonStyle.secondary, row=3)
    async def brand_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(_BrandSearchModal(self.state, self))

    @discord.ui.button(label="📝 Mots-clés & Prix", style=discord.ButtonStyle.secondary, row=3)
    async def prix_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(_KeywordsPrixModal(self.state, self))

    @discord.ui.button(label="📂 Catégorie",        style=discord.ButtonStyle.secondary, row=3)
    async def cat_btn(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        cats = await self.state.bot.vinted.get_catalogs(self.state.domain)
        view = _CatalogSelectView(self.state, self, cats[:25])
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="📂 Choisissez une catégorie",
                description="Sélectionnez une catégorie Vinted puis cliquez sur ← Retour.",
                color=_VINTED_GREEN,
            ),
            view=view,
        )

    @discord.ui.button(label="✅ Créer le filtre",  style=discord.ButtonStyle.success, row=4)
    async def save_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        s = self.state
        filter_id = await s.bot.db.add_filter(
            guild_id=str(s.guild_id),
            channel_id=str(s.channel_id),
            name=s.nom,
            search_text=s.search_text,
            min_price=s.prix_min,
            max_price=s.prix_max,
            brand_ids=s.brand_id,
            brand_titles=s.brand_title,
            size_ids=",".join(s.size_ids),
            size_titles=",".join(s.size_titles),
            catalog_ids=s.cat_id,
            catalog_titles=s.cat_title.strip(),
            condition_ids=",".join(s.condition_ids),
            domain=s.domain,
        )
        for child in self.children:
            child.disabled = True
        btn.label = f"✅ Filtre #{filter_id} créé !"

        embed = s.summary_embed(title_prefix="✅")
        embed.color = discord.Color.green()
        embed.set_footer(
            text=f"Filtre #{filter_id} actif — premier sondage dans ~90s"
        )
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    async def on_timeout(self):
        if self.state.message:
            for c in self.children:
                c.disabled = True
            try:
                await self.state.message.edit(view=self)
            except Exception:
                pass


# ── Selects ───────────────────────────────────────────────────────────────────

class _PaysSelect(discord.ui.Select):
    def __init__(self, state: FilterState, view: FilterSetupView):
        self._state = state
        self._view = view
        options = [
            discord.SelectOption(label=lbl, value=domain, default=(domain == state.domain))
            for lbl, domain in VINTED_DOMAINS.items()
        ]
        super().__init__(placeholder="🌐 Pays Vinted…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self._state.domain = self.values[0]
        for opt in self.options:
            opt.default = (opt.value == self.values[0])
        await interaction.response.edit_message(
            embed=self._state.summary_embed(), view=self._view
        )


class _EtatSelect(discord.ui.Select):
    def __init__(self, state: FilterState, view: FilterSetupView):
        self._state = state
        self._view = view
        options = [
            discord.SelectOption(label=label, value=cid, emoji=label[0])
            for label, cid in CONDITIONS
        ]
        super().__init__(
            placeholder="🏷️ État(s) de l'article — sélection multiple possible",
            options=options,
            min_values=0,
            max_values=len(options),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self._state.condition_ids = list(self.values)
        await interaction.response.edit_message(
            embed=self._state.summary_embed(), view=self._view
        )


class _TailleSelect(discord.ui.Select):
    def __init__(self, state: FilterState, view: FilterSetupView, sizes: list[dict]):
        self._state = state
        self._view = view
        # Limiter à 25 options max (contrainte Discord)
        sizes = sizes[:25]
        options = [
            discord.SelectOption(label=s["title"][:100], value=f"{s['id']}|{s['title']}"[:100])
            for s in sizes
        ]
        super().__init__(
            placeholder="📏 Taille(s) — sélection multiple possible",
            options=options,
            min_values=0,
            max_values=len(options),
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        ids, titles = [], []
        for v in self.values:
            if "|" in v:
                sid, stitle = v.split("|", 1)
                ids.append(sid)
                titles.append(stitle)
        self._state.size_ids = ids
        self._state.size_titles = titles
        await interaction.response.edit_message(
            embed=self._state.summary_embed(), view=self._view
        )


# ── Modaux ────────────────────────────────────────────────────────────────────

class _BrandSearchModal(discord.ui.Modal, title="🔍 Rechercher une marque"):
    query = discord.ui.TextInput(
        label="Nom de la marque",
        placeholder="ex: Nike, Zara, Adidas, The North Face…",
        min_length=2,
        max_length=50,
    )

    def __init__(self, state: FilterState, parent: FilterSetupView):
        super().__init__()
        self._state = state
        self._parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        brands = await self._state.bot.vinted.search_brands(
            self.query.value, self._state.domain
        )
        if not brands:
            embed = discord.Embed(
                title=f"❌ Aucune marque pour « {self.query.value} »",
                description="Essayez un autre terme (ex: marque en anglais, sans accent).",
                color=discord.Color.red(),
            )
            view = _BackOnlyView(self._state, self._parent)
            await interaction.edit_original_response(embed=embed, view=view)
            return

        view = _BrandSelectView(self._state, self._parent, brands[:25])
        embed = discord.Embed(
            title=f"👟 Résultats pour « {self.query.value} »",
            description=f"{len(brands)} marque(s) — choisissez-en une, ou cliquez Retour.",
            color=_VINTED_GREEN,
        )
        await interaction.edit_original_response(embed=embed, view=view)


class _KeywordsPrixModal(discord.ui.Modal, title="📝 Mots-clés & Prix"):
    search = discord.ui.TextInput(
        label="Mots-clés de recherche",
        placeholder="ex: veste cuir noir, sneakers Jordan…",
        required=False,
        max_length=200,
    )
    prix_min = discord.ui.TextInput(
        label="Prix minimum (€)",
        placeholder="ex: 10",
        required=False,
        max_length=8,
    )
    prix_max = discord.ui.TextInput(
        label="Prix maximum (€)",
        placeholder="ex: 100",
        required=False,
        max_length=8,
    )

    def __init__(self, state: FilterState, parent: FilterSetupView):
        super().__init__()
        self._state = state
        self._parent = parent
        if state.search_text:
            self.search.default = state.search_text
        if state.prix_min is not None:
            self.prix_min.default = str(int(state.prix_min))
        if state.prix_max is not None:
            self.prix_max.default = str(int(state.prix_max))

    async def on_submit(self, interaction: discord.Interaction):
        self._state.search_text = self.search.value.strip()
        try:
            v = self.prix_min.value.strip().replace(",", ".")
            self._state.prix_min = float(v) if v else None
        except ValueError:
            self._state.prix_min = None
        try:
            v = self.prix_max.value.strip().replace(",", ".")
            self._state.prix_max = float(v) if v else None
        except ValueError:
            self._state.prix_max = None
        # Les modaux ne peuvent pas faire edit_message — on édite le message stocké
        await interaction.response.defer()
        await self._state.message.edit(
            embed=self._state.summary_embed(), view=self._parent
        )


# ── Vues secondaires ─────────────────────────────────────────────────────────

class _BrandSelectView(discord.ui.View):
    def __init__(self, state: FilterState, parent: FilterSetupView, brands: list[dict]):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(
                label=b["title"][:100],
                value=f"{b['id']}|{b['title']}"[:100],
            )
            for b in brands
        ]
        # Ajouter option "Aucune marque" en tête
        options.insert(0, discord.SelectOption(label="✖ Aucune marque (retirer)", value="clear"))
        self.add_item(_BrandSelect(state, parent, options))
        self.add_item(_BackButton(state, parent))


class _BrandSelect(discord.ui.Select):
    def __init__(self, state: FilterState, parent: FilterSetupView, options):
        self._state = state
        self._parent = parent
        super().__init__(
            placeholder="Choisissez une marque…",
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        raw = self.values[0]
        if raw == "clear":
            self._state.brand_id = ""
            self._state.brand_title = ""
        elif "|" in raw:
            bid, btitle = raw.split("|", 1)
            self._state.brand_id = bid
            self._state.brand_title = btitle
        await interaction.response.edit_message(
            embed=self._state.summary_embed(), view=self._parent
        )


class _CatalogSelectView(discord.ui.View):
    def __init__(self, state: FilterState, parent: FilterSetupView, cats: list[dict]):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(
                label=c["title"][:100],
                value=f"{c['id']}|{c['title']}"[:100],
            )
            for c in cats
        ]
        options.insert(0, discord.SelectOption(label="✖ Toutes catégories (retirer)", value="clear"))
        self.add_item(_CatalogSelect(state, parent, options))
        self.add_item(_BackButton(state, parent))


class _CatalogSelect(discord.ui.Select):
    def __init__(self, state: FilterState, parent: FilterSetupView, options):
        self._state = state
        self._parent = parent
        super().__init__(placeholder="Choisissez une catégorie…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        raw = self.values[0]
        if raw == "clear":
            self._state.cat_id = ""
            self._state.cat_title = ""
        elif "|" in raw:
            cid, ctitle = raw.split("|", 1)
            self._state.cat_id = cid
            self._state.cat_title = ctitle.strip()
        await interaction.response.edit_message(
            embed=self._state.summary_embed(), view=self._parent
        )


class _BackButton(discord.ui.Button):
    def __init__(self, state: FilterState, parent: FilterSetupView):
        super().__init__(label="← Retour", style=discord.ButtonStyle.secondary, row=1)
        self._state = state
        self._parent = parent

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=self._state.summary_embed(), view=self._parent
        )


class _BackOnlyView(discord.ui.View):
    def __init__(self, state: FilterState, parent: FilterSetupView):
        super().__init__(timeout=60)
        self.add_item(_BackButton(state, parent))


# ── Bot ───────────────────────────────────────────────────────────────────────

class VintedBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = Config()
        self.db = Database(self.config.DATABASE_PATH)
        self.vinted = VintedClient()
        self.embeds = EmbedBuilder()
        self.monitor = Monitor(self)
        # Tailles pré-chargées (mis à jour dans on_ready)
        self.cached_sizes: list[dict] = []

    async def setup_hook(self):
        await self.db.connect()
        _register_commands(self)
        await self.tree.sync()
        logger.info("Slash commands synchronisées")

    async def on_ready(self):
        logger.info(f"Connecté : {self.user} (ID={self.user.id})")
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="Vinted 🛍️")
        )
        # Initialisation asynchrone (évite les timeouts au premier usage)
        import asyncio
        asyncio.create_task(self._startup_tasks())

    async def _startup_tasks(self):
        import asyncio
        await self.vinted.prewarm("www.vinted.fr")
        self.cached_sizes = await self.vinted.get_sizes("www.vinted.fr")
        await self.monitor.start()
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


# ── Commandes slash ───────────────────────────────────────────────────────────

def _register_commands(bot: VintedBot):

    # ── /ajouter-filtre ───────────────────────────────────────────────────────
    @bot.tree.command(
        name="ajouter-filtre",
        description="Créer un filtre de surveillance Vinted (menu guidé)",
    )
    @app_commands.describe(
        nom="Nom du filtre (ex: Nike Air Max 42)",
        salon="Salon Discord qui recevra les notifications",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def cmd_add_filter(
        interaction: discord.Interaction,
        nom: str,
        salon: discord.TextChannel,
    ):
        await interaction.response.defer(ephemeral=True)

        state = FilterState(
            bot=bot,
            guild_id=interaction.guild_id,
            channel_id=salon.id,
            nom=nom,
        )
        # Utiliser les tailles pre-chargées (ou fallback si pas encore dispo)
        sizes = bot.cached_sizes or await bot.vinted.get_sizes()

        view = FilterSetupView(state, sizes)
        msg = await interaction.followup.send(
            embed=state.summary_embed(title_prefix="⚙️ Configuration :"),
            view=view,
            ephemeral=True,
            wait=True,
        )
        state.message = msg
        view.state.message = msg

    # ── /liste-filtres ────────────────────────────────────────────────────────
    @bot.tree.command(name="liste-filtres", description="Voir tous les filtres actifs sur ce serveur")
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
            await interaction.response.send_message("❌ Filtre introuvable.", ephemeral=True)
            return
        await bot.db.delete_filter(id, str(interaction.guild_id))
        await interaction.response.send_message(
            f"✅ Filtre **{row['name']}** (ID `#{id}`) supprimé.", ephemeral=True
        )

    # ── /tester-filtre ────────────────────────────────────────────────────────
    @bot.tree.command(
        name="tester-filtre",
        description="Tester un filtre — affiche 3 articles sans les marquer comme vus",
    )
    @app_commands.describe(id="ID du filtre à tester")
    async def cmd_test_filter(interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)
        row = await bot.db.get_filter(id)
        if row is None or str(row["guild_id"]) != str(interaction.guild_id):
            await interaction.followup.send("❌ Filtre introuvable.", ephemeral=True)
            return
        fd = dict(row)
        items = await bot.vinted.search(fd.get("domain", "www.vinted.fr"), _build_params(fd))
        if not items:
            await interaction.followup.send("⚠️ Aucun résultat ou erreur Vinted.", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ **{len(items)} articles** trouvés pour **{fd['name']}** — aperçu des 3 premiers :",
            ephemeral=True,
        )
        for item in items[:3]:
            await interaction.followup.send(
                embed=bot.embeds.build_item_embed(item, fd), ephemeral=True
            )

    # ── /aide ─────────────────────────────────────────────────────────────────
    @bot.tree.command(name="aide", description="Afficher l'aide du bot Vinted")
    async def cmd_help(interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=bot.embeds.build_help_embed(), ephemeral=True
        )

    # ── Erreurs ───────────────────────────────────────────────────────────────
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
                await interaction.response.send_message("❌ Erreur inattendue.", ephemeral=True)
