"""DM delivery of the alerts, with the persistent buttons of a replacement suggestion."""

import logging
import re
from typing import TYPE_CHECKING

import discord

from monatrain.models import TrainInfo
from monatrain.timeutil import PARIS

if TYPE_CHECKING:
    from monatrain.bot.client import MonatrainBot

log = logging.getLogger(__name__)


class ReplaceButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"replace:(?P<follow_id>\d+):(?P<hhmm>[01]\d:[0-5]\d|2[0-3]:[0-5]\d)",
):
    """Button "Suivre HH:MM": the custom_id carries everything, so it keeps working after a restart."""

    def __init__(self, follow_id: int, hhmm: str) -> None:
        super().__init__(
            discord.ui.Button(
                label=f"Suivre {hhmm}", style=discord.ButtonStyle.success, custom_id=f"replace:{follow_id}:{hhmm}"
            )
        )
        self.follow_id = follow_id
        self.hhmm = hhmm

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["follow_id"]), match["hhmm"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: MonatrainBot = interaction.client  # type: ignore[assignment]
        await interaction.response.defer()  # resolving the train may call the SNCF API
        text = await bot.replace_follow(str(interaction.user.id), self.follow_id, self.hhmm)
        await _drop_buttons(interaction)
        await interaction.followup.send(text)


class KeepButton(discord.ui.DynamicItem[discord.ui.Button], template=r"keep:(?P<follow_id>\d+)"):
    """Button "Garder l'actuel": nothing to change, the buttons just go away."""

    def __init__(self, follow_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Garder l'actuel", style=discord.ButtonStyle.secondary, custom_id=f"keep:{follow_id}"
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["follow_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=None)
        await interaction.followup.send("👍 Ok, je garde le train actuel.")


async def _drop_buttons(interaction: discord.Interaction) -> None:
    try:
        await interaction.edit_original_response(view=None)
    except discord.HTTPException:
        log.warning("Boutons de la suggestion non retirés", exc_info=True)


def suggestion_view(follow_id: int, train: TrainInfo) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReplaceButton(follow_id, train.base_departure.astimezone(PARIS).strftime("%H:%M")))
    view.add_item(KeepButton(follow_id))
    return view


class DiscordNotifier:
    """Implements the watcher's Notifier protocol with Discord DMs."""

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def send(self, user_id: str, text: str, *, suggestion: tuple[int, TrainInfo] | None = None) -> bool:
        try:
            user = await self._client.fetch_user(int(user_id))
            if suggestion is None:
                await user.send(text)
            else:
                await user.send(text, view=suggestion_view(*suggestion))
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            log.warning("DM impossible pour l'utilisateur %s : %s", user_id, exc)
            return False
        return True
