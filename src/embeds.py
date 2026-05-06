"""
Construction des embeds Discord pour les articles Vinted.
"""

import discord
from datetime import datetime

# Couleur verte Vinted
_VINTED_COLOR = discord.Color.from_rgb(9, 182, 109)

_CURRENCY_SYMBOLS: dict[str, str] = {
    "EUR": "€",
    "GBP": "£",
    "PLN": "zł",
    "CZK": "Kč",
    "HUF": "Ft",
    "RON": "lei",
}

_CONDITIONS: dict[str, str] = {
    "new_with_tags":    "🏷️ Neuf avec étiquettes",
    "new_without_tags": "✨ Neuf sans étiquettes",
    "very_good":        "👍 Très bon état",
    "good":             "👌 Bon état",
    "satisfactory":     "🙂 État satisfaisant",
}


def _str(value) -> str:
    """Convertit une valeur en str propre, retourne '' si None/vide."""
    return str(value).strip() if value else ""


def _photo_url(item: dict) -> str:
    """Extrait l'URL de la première photo disponible."""
    # Format v2 : item.photos[] ou item.photo
    photos = item.get("photos") or []
    if photos:
        p = photos[0]
        return (
            p.get("full_size_url")
            or p.get("url")
            or (p.get("thumbnails") or [{}])[0].get("url", "")
        )
    photo = item.get("photo") or {}
    return photo.get("full_size_url") or photo.get("url", "")


def _item_url(item: dict, domain: str) -> str:
    raw = _str(item.get("url"))
    if raw.startswith("http"):
        return raw
    if raw:
        return f"https://{domain}{raw}"
    # Fallback : construire l'URL depuis l'ID
    item_id = item.get("id")
    if item_id:
        return f"https://{domain}/items/{item_id}"
    return f"https://{domain}"


class EmbedBuilder:

    def build_item_embed(self, item: dict, filter_data: dict) -> discord.Embed:
        domain = filter_data.get("domain") or "www.vinted.fr"
        title = _str(item.get("title")) or "Article sans titre"
        url = _item_url(item, domain)

        embed = discord.Embed(
            title=title[:256],
            url=url,
            color=_VINTED_COLOR,
            timestamp=datetime.utcnow(),
        )

        # ── Prix ──────────────────────────────────────────────────────────────
        price_obj = item.get("price") or {}
        if isinstance(price_obj, dict):
            amount = _str(price_obj.get("amount"))
            currency = _str(price_obj.get("currency_code")) or "EUR"
        else:
            amount = _str(price_obj)
            currency = "EUR"
        symbol = _CURRENCY_SYMBOLS.get(currency, currency)
        embed.add_field(
            name="Prix",
            value=f"**{amount} {symbol}**" if amount else "Non renseigné",
            inline=True,
        )

        # ── Taille ────────────────────────────────────────────────────────────
        size = _str(item.get("size_title"))
        if not size:
            size_obj = item.get("size") or {}
            size = _str(size_obj.get("title") if isinstance(size_obj, dict) else size_obj)
        if size:
            embed.add_field(name="Taille", value=size, inline=True)

        # ── Marque ────────────────────────────────────────────────────────────
        brand = _str(item.get("brand_title"))
        if not brand:
            brand_obj = item.get("brand") or {}
            brand = _str(brand_obj.get("title") if isinstance(brand_obj, dict) else brand_obj)
        if brand:
            embed.add_field(name="Marque", value=brand, inline=True)

        # ── État ──────────────────────────────────────────────────────────────
        condition_key = _str(item.get("status"))
        condition = _CONDITIONS.get(condition_key, condition_key.replace("_", " ").capitalize())
        if condition:
            embed.add_field(name="État", value=condition, inline=True)

        # ── Lieu ──────────────────────────────────────────────────────────────
        city = _str(item.get("city"))
        country = _str(item.get("country_title"))
        location = ", ".join(filter(None, [city, country]))
        if location:
            embed.add_field(name="Lieu", value=location, inline=True)

        # ── Vendeur ───────────────────────────────────────────────────────────
        user_obj = item.get("user") or {}
        if isinstance(user_obj, dict):
            login = _str(user_obj.get("login"))
            feedback = user_obj.get("feedback_reputation")
            seller_val = login
            if feedback is not None:
                try:
                    pct = round(float(feedback) * 100)
                    seller_val = f"{login} ({pct}% 👍)"
                except (ValueError, TypeError):
                    pass
            if seller_val:
                embed.add_field(name="Vendeur", value=seller_val, inline=True)

        # ── Image ─────────────────────────────────────────────────────────────
        img = _photo_url(item)
        if img:
            embed.set_image(url=img)

        embed.set_footer(text=f"Filtre : {filter_data.get('name', '?')}  •  Vinted")
        return embed

    # ── Liste des filtres ──────────────────────────────────────────────────────

    def build_filter_list_embed(self, filters: list[dict], guild_name: str) -> discord.Embed:
        embed = discord.Embed(
            title=f"Filtres Vinted — {guild_name}",
            color=_VINTED_COLOR,
        )

        if not filters:
            embed.description = (
                "Aucun filtre configuré.\n"
                "Utilisez `/ajouter-filtre` pour créer votre première surveillance."
            )
            return embed

        for f in filters:
            lines = []
            if f.get("search_text"):
                lines.append(f"🔍 `{f['search_text']}`")
            if f.get("min_price") is not None or f.get("max_price") is not None:
                lo = f"{f['min_price']}€" if f.get("min_price") is not None else "0€"
                hi = f"{f['max_price']}€" if f.get("max_price") is not None else "∞"
                lines.append(f"💰 {lo} – {hi}")
            if f.get("brand_ids"):
                lines.append(f"👟 Marques IDs : `{f['brand_ids']}`")
            if f.get("size_ids"):
                lines.append(f"📏 Tailles IDs : `{f['size_ids']}`")
            lines.append(f"📢 <#{f['channel_id']}>")
            lines.append(f"🌐 {f['domain']}")

            embed.add_field(
                name=f"#{f['id']} — {f['name']}",
                value="\n".join(lines),
                inline=False,
            )

        return embed

    # ── Aide ──────────────────────────────────────────────────────────────────

    def build_help_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="📦 VintedBot — Aide",
            description=(
                "Ce bot surveille Vinted et envoie une notification dans vos salons "
                "dès qu'un article correspondant à vos filtres est publié."
            ),
            color=_VINTED_COLOR,
        )
        embed.add_field(
            name="/ajouter-filtre",
            value=(
                "Crée une surveillance pour un mot-clé, une fourchette de prix, "
                "sur le pays Vinted de votre choix."
            ),
            inline=False,
        )
        embed.add_field(
            name="/liste-filtres",
            value="Affiche tous les filtres actifs sur ce serveur.",
            inline=False,
        )
        embed.add_field(
            name="/supprimer-filtre `id`",
            value="Supprime un filtre par son identifiant.",
            inline=False,
        )
        embed.add_field(
            name="/tester-filtre `id`",
            value="Affiche les 3 derniers articles correspondant à un filtre (sans marquer comme vu).",
            inline=False,
        )
        embed.add_field(
            name="/aide",
            value="Affiche ce message.",
            inline=False,
        )
        embed.set_footer(text="Vinted Bot • surveillance respectueuse des limites de l'API")
        return embed
