"""
Bot Discord — UI guidée avec menus interactifs, multi-sélection, modaux.

Flux /ajouter-filtre :
  1. Commande slash (nom + salon obligatoires)
  2. Embed de configuration + composants interactifs :
     - Select Pays           (1 choix)
     - Select État(s)        (0-5 choix simultanés)
     - Select Taille(s)      (0-N choix simultanés)
     - Bouton Marque(s)      → Modal recherche → Select multi-marques
     - Bouton Mots-clés      → Modal (recherche libre + prix)
     - Bouton Catégorie      → Select catégories
     - Bouton Créer ✅       → sauvegarde + confirme

Conception :
  - FilterState centralise tout l'état + les sizes pour recréer les vues
  - Chaque retour au menu principal crée une NOUVELLE FilterSetupView
    (évite les tokens Discord expirés sur l'ancienne vue)
  - Multi-marques : le Select de résultats permet de cocher plusieurs marques
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

CONDITIONS: list[tuple[str, str]] = [
    ("🏷️ Neuf avec étiquettes",  "6"),
    ("✨ Neuf sans étiquettes", "1"),
    ("👍 Très bon état",        "2"),
    ("👌 Bon état",             "3"),
    ("🙂 État satisfaisant",    "4"),
]
CONDITION_LABELS: dict[str, str] = {cid: label for label, cid in CONDITIONS}

_VINTED_GREEN = discord.Color.from_rgb(9, 182, 109)


# ── État du filtre en cours de configuration ──────────────────────────────────

class FilterState:
    """
    Tout l'état du filtre en cours de création.
    Aussi stocké ici : sizes (pour recréer FilterSetupView sans perdre l'état).
    """

    def __init__(self, bot: "VintedBot", guild_id: int, channel_id: int, nom: str,
                 sizes: list[dict]):
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.nom = nom
        self.sizes = sizes                      # gardé pour recréer la vue
        self.message: Optional[discord.Message] = None

        # Critères
        self.domain = "www.vinted.fr"
        self.search_text = ""
        self.prix_min: Optional[float] = None
        self.prix_max: Optional[float] = None
        self.brand_ids: list[str] = []          # IDs Vinted (vides = mode texte)
        self.brand_titles: list[str] = []       # noms affichés
        self.cat_id = ""
        self.cat_title = ""
        self.condition_ids: list[str] = []
        self.size_ids: list[str] = []
        self.size_titles: list[str] = []

    # ── Résumé ────────────────────────────────────────────────────────────────

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
        embed.add_field(name="🌐 Pays",   value=domain_label,  inline=True)
        embed.add_field(
            name="🏷️ État(s)",
            value=", ".join(CONDITION_LABELS[c] for c in self.condition_ids) or "*(tous)*",
            inline=True,
        )
        embed.add_field(
            name="👟 Marque(s)",
            value=", ".join(self.brand_titles) or "*(toutes)*",
            inline=True,
        )
        embed.add_field(
            name="📂 Catégorie",
            value=self.cat_title.strip() or "*(toutes)*",
            inline=True,
        )
        embed.add_field(
            name="📏 Taille(s)",
            value=", ".join(self.size_titles) or "*(toutes)*",
            inline=True,
        )
        embed.add_field(
            name="🔍 Mots-clés",
            value=f"`{self.search_text}`" if self.search_text else "*(aucun)*",
            inline=True,
        )
        if self.prix_min is not None or self.prix_max is not None:
            lo = f"{self.prix_min}€" if self.prix_min is not None else "0€"
            hi = f"{self.prix_max}€" if self.prix_max is not None else "∞"
            embed.add_field(name="💰 Prix", value=f"{lo} – {hi}", inline=True)
        embed.set_footer(text="Modifiez les critères puis cliquez sur ✅ Créer le filtre")
        return embed

    # ── Factory de vue fraîche ────────────────────────────────────────────────

    def fresh_main_view(self) -> "FilterSetupView":
        """
        Crée une nouvelle FilterSetupView avec l'état actuel.
        À utiliser à chaque retour au menu principal pour éviter les tokens Discord
        expirés sur l'ancienne instance de vue.
        """
        return FilterSetupView(self)


# ── Vue principale ────────────────────────────────────────────────────────────

class FilterSetupView(discord.ui.View):
    def __init__(self, state: FilterState):
        super().__init__(timeout=600)           # 10 minutes
        self.state = state
        self.add_item(_PaysSelect(state))
        self.add_item(_EtatSelect(state))
        self.add_item(_TailleSelect(state))

    @discord.ui.button(label="👟 Marque(s)",        style=discord.ButtonStyle.secondary, row=3)
    async def brand_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(_BrandSearchModal(self.state))

    @discord.ui.button(label="📝 Mots-clés & Prix", style=discord.ButtonStyle.secondary, row=3)
    async def prix_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(_KeywordsPrixModal(self.state))

    @discord.ui.button(label="📂 Catégorie",        style=discord.ButtonStyle.secondary, row=3)
    async def cat_btn(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        cats = await self.state.bot.vinted.get_catalogs(self.state.domain)
        view = _CatalogSelectView(self.state, cats[:25])
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="📂 Catégorie",
                description="Choisissez une catégorie, puis **← Retour** pour continuer.",
                color=_VINTED_GREEN,
            ),
            view=view,
        )

    @discord.ui.button(label="✅ Créer le filtre", style=discord.ButtonStyle.success, row=4)
    async def save_btn(self, interaction: discord.Interaction, btn: discord.ui.Button):
        s = self.state
        filter_id = await s.bot.db.add_filter(
            guild_id=str(s.guild_id),
            channel_id=str(s.channel_id),
            name=s.nom,
            search_text=s.search_text,
            min_price=s.prix_min,
            max_price=s.prix_max,
            brand_ids=",".join(b for b in s.brand_ids if b),
            brand_titles=",".join(s.brand_titles),
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
        embed.set_footer(text=f"Filtre #{filter_id} actif — premier sondage dans ~90s")
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


# ── Helper : retour au menu principal ────────────────────────────────────────

async def _back_to_main(interaction: discord.Interaction, state: FilterState):
    """
    Retourne au menu principal en créant une NOUVELLE vue (token Discord frais).
    Appelé par tous les boutons/selects de sous-vues.
    """
    view = state.fresh_main_view()
    await interaction.response.edit_message(embed=state.summary_embed(), view=view)


# ── Selects principaux ────────────────────────────────────────────────────────

class _PaysSelect(discord.ui.Select):
    def __init__(self, state: FilterState):
        self._state = state
        options = [
            discord.SelectOption(label=lbl, value=domain,
                                 default=(domain == state.domain))
            for lbl, domain in VINTED_DOMAINS.items()
        ]
        super().__init__(placeholder="🌐 Pays Vinted…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self._state.domain = self.values[0]
        await _back_to_main(interaction, self._state)


class _EtatSelect(discord.ui.Select):
    def __init__(self, state: FilterState):
        self._state = state
        options = [
            discord.SelectOption(label=label, value=cid)
            for label, cid in CONDITIONS
        ]
        super().__init__(
            placeholder="🏷️ État(s) — sélection multiple",
            options=options,
            min_values=0,
            max_values=len(options),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self._state.condition_ids = list(self.values)
        await _back_to_main(interaction, self._state)


class _TailleSelect(discord.ui.Select):
    def __init__(self, state: FilterState):
        self._state = state
        sizes = state.sizes[:25]
        options = [
            discord.SelectOption(
                label=s["title"][:100],
                value=f"{s['id']}|{s['title']}"[:100],
            )
            for s in sizes
        ]
        super().__init__(
            placeholder="📏 Taille(s) — sélection multiple",
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
        await _back_to_main(interaction, self._state)


# ── Modaux ────────────────────────────────────────────────────────────────────

class _BrandSearchModal(discord.ui.Modal, title="🔍 Rechercher des marques"):
    query = discord.ui.TextInput(
        label="Nom de la marque",
        placeholder="ex: Nike, Zara, Adidas…",
        min_length=2,
        max_length=50,
    )

    def __init__(self, state: FilterState):
        super().__init__()
        self._state = state

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        brands = await self._state.bot.vinted.search_brands(
            self.query.value, self._state.domain
        )
        if not brands:
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title=f"❌ Aucune marque pour « {self.query.value} »",
                    description=(
                        "Essayez un autre terme ou vérifiez l'orthographe.\n"
                        "Cliquez sur **← Retour** pour continuer sans marque."
                    ),
                    color=discord.Color.red(),
                ),
                view=_BackOnlyView(self._state),
            )
            return

        view = _BrandSelectView(self._state, brands[:25])
        embed = discord.Embed(
            title=f"👟 « {self.query.value} » — {len(brands)} résultat(s)",
            description=(
                "Sélectionnez **une ou plusieurs marques**, puis cliquez sur **✔ Valider**.\n"
                "Les marques déjà choisies seront **conservées** (ajout cumulatif)."
            ),
            color=_VINTED_GREEN,
        )
        # Rappeler les marques déjà sélectionnées
        if self._state.brand_titles:
            embed.add_field(
                name="Déjà sélectionné",
                value=", ".join(self._state.brand_titles),
                inline=False,
            )
        await interaction.edit_original_response(embed=embed, view=view)


class _KeywordsPrixModal(discord.ui.Modal, title="📝 Mots-clés & Prix"):
    search = discord.ui.TextInput(
        label="Mots-clés de recherche",
        placeholder="ex: veste cuir noir, sneakers…",
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

    def __init__(self, state: FilterState):
        super().__init__()
        self._state = state
        if state.search_text:
            self.search.default = state.search_text
        if state.prix_min is not None:
            self.prix_min.default = str(int(state.prix_min))
        if state.prix_max is not None:
            self.prix_max.default = str(int(state.prix_max))

    async def on_submit(self, interaction: discord.Interaction):
        self._state.search_text = self.search.value.strip()
        for attr, field in [("prix_min", self.prix_min), ("prix_max", self.prix_max)]:
            v = field.value.strip().replace(",", ".")
            try:
                setattr(self._state, attr, float(v) if v else None)
            except ValueError:
                setattr(self._state, attr, None)
        # Modal → pas de edit_message direct, on édite via le message stocké
        await interaction.response.defer()
        await self._state.message.edit(
            embed=self._state.summary_embed(),
            view=self._state.fresh_main_view(),
        )


# ── Vues secondaires ──────────────────────────────────────────────────────────

class _BrandSelectView(discord.ui.View):
    """Affiche les résultats d'une recherche de marque — sélection multiple."""

    def __init__(self, state: FilterState, brands: list[dict]):
        super().__init__(timeout=180)
        self._state = state
        self._pending: list[tuple[str, str]] = []  # (id, title) sélectionnés dans ce Select

        options = [
            discord.SelectOption(
                label=b["title"][:100],
                value=f"{b['id']}|{b['title']}"[:100],
                # Cocher les marques déjà dans l'état
                default=(b["title"] in state.brand_titles),
            )
            for b in brands
        ]
        self._brand_select = _BrandSelect(state, options)
        self.add_item(self._brand_select)

    @discord.ui.button(label="✔ Valider la sélection", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction, _):
        """Ajoute les marques cochées à l'état (cumulatif) et retourne au menu."""
        for v in self._brand_select.values:
            if "|" in v:
                bid, btitle = v.split("|", 1)
                # Éviter les doublons
                if btitle not in self._state.brand_titles:
                    self._state.brand_ids.append(bid if bid != "0" else "")
                    self._state.brand_titles.append(btitle)
        await _back_to_main(interaction, self._state)

    @discord.ui.button(label="🗑 Retirer toutes les marques", style=discord.ButtonStyle.danger, row=1)
    async def clear_brands(self, interaction: discord.Interaction, _):
        self._state.brand_ids = []
        self._state.brand_titles = []
        await _back_to_main(interaction, self._state)

    @discord.ui.button(label="← Retour sans changer", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, _):
        await _back_to_main(interaction, self._state)


class _BrandSelect(discord.ui.Select):
    def __init__(self, state: FilterState, options: list):
        self._state = state
        super().__init__(
            placeholder="Cochez une ou plusieurs marques…",
            options=options,
            min_values=0,
            max_values=len(options),
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        # Ne pas agir ici — l'action se fait sur "✔ Valider"
        await interaction.response.defer()


class _CatalogSelectView(discord.ui.View):
    def __init__(self, state: FilterState, cats: list[dict]):
        super().__init__(timeout=180)
        self._state = state
        options = [
            discord.SelectOption(
                label=c["title"][:100],
                value=f"{c['id']}|{c['title']}"[:100],
                default=(str(c["id"]) == state.cat_id),
            )
            for c in cats
        ]
        options.insert(0, discord.SelectOption(label="✖ Toutes catégories", value="clear"))
        self.add_item(_CatalogSelect(state, options))

    @discord.ui.button(label="← Retour", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _):
        await _back_to_main(interaction, self._state)


class _CatalogSelect(discord.ui.Select):
    def __init__(self, state: FilterState, options: list):
        self._state = state
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
        await _back_to_main(interaction, self._state)


class _BackOnlyView(discord.ui.View):
    def __init__(self, state: FilterState):
        super().__init__(timeout=120)
        self._state = state

    @discord.ui.button(label="← Retour", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _):
        await _back_to_main(interaction, self._state)


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
        import asyncio
        asyncio.create_task(self._startup_tasks())

    async def _startup_tasks(self):
        import asyncio
        await self.vinted.prewarm("www.vinted.fr")
        self.cached_sizes = await self.vinted.get_sizes("www.vinted.fr")
        logger.info(f"{len(self.cached_sizes)} tailles chargées")
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


# ── Commandes slash ───────────────────────────────────────────────────────────

def _register_commands(bot: VintedBot):

    @bot.tree.command(
        name="ajouter-filtre",
        description="Créer un filtre de surveillance Vinted (menu guidé interactif)",
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
        sizes = bot.cached_sizes or await bot.vinted.get_sizes()
        state = FilterState(
            bot=bot,
            guild_id=interaction.guild_id,
            channel_id=salon.id,
            nom=nom,
            sizes=sizes,
        )
        view = FilterSetupView(state)
        msg = await interaction.followup.send(
            embed=state.summary_embed(title_prefix="⚙️ Configuration :"),
            view=view,
            ephemeral=True,
            wait=True,
        )
        state.message = msg

    @bot.tree.command(name="liste-filtres", description="Voir tous les filtres actifs")
    async def cmd_list_filters(interaction: discord.Interaction):
        rows = await bot.db.get_filters(str(interaction.guild_id))
        embed = bot.embeds.build_filter_list_embed(
            [dict(r) for r in rows], interaction.guild.name
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="supprimer-filtre",
                      description="Supprimer un filtre (IDs via /liste-filtres)")
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

    @bot.tree.command(name="tester-filtre",
                      description="Tester un filtre — affiche 3 articles sans les marquer comme vus")
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
            f"✅ **{len(items)} articles** trouvés — aperçu des 3 premiers :", ephemeral=True
        )
        for item in items[:3]:
            await interaction.followup.send(
                embed=bot.embeds.build_item_embed(item, fd), ephemeral=True
            )

    @bot.tree.command(name="aide", description="Afficher l'aide du bot Vinted")
    async def cmd_help(interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=bot.embeds.build_help_embed(), ephemeral=True
        )

    @cmd_add_filter.error
    @cmd_delete_filter.error
    async def _perm_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ Tu as besoin de la permission **Gérer les salons**.", ephemeral=True
            )
        else:
            logger.exception(f"Erreur slash command : {error}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Erreur inattendue.", ephemeral=True)
