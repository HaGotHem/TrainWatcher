"""The Discord client: slash commands, persistent buttons and the watcher running in the background."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

import discord
from discord import app_commands

from monatrain.bot.handlers import (
    MSG_NOT_YOURS,
    MSG_RATE,
    Choices,
    autocomplete_gare,
    autocomplete_heure,
    autocomplete_mes_trains,
    dm_warning,
    handle_departs,
    handle_mes_trains,
    handle_remplacer,
    handle_retirer,
    handle_suivre,
)
from monatrain.bot.notifier import DiscordNotifier, KeepButton, ReplaceButton
from monatrain.ratelimit import SlidingWindowLimiter
from monatrain.repo import Repo
from monatrain.timeutil import now_paris
from monatrain.watcher import Watcher

log = logging.getLogger(__name__)

USER_RATE_LIMIT = 20  # interactions calling the API, per Discord user
USER_RATE_WINDOW_S = 600
WELCOME_DM = "Tes alertes arriveront ici 🚆"
MSG_ERROR = "Une erreur est survenue."
INSTALL_URL = (
    "https://discord.com/oauth2/authorize?client_id={client_id}&integration_type=1&scope=applications.commands"
)

JOURS = [app_commands.Choice(name=name, value=name) for name in ("semaine", "tous", "week-end", "perso")]


def _choices(pairs: Choices) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=name[:100], value=value) for name, value in pairs]


class MonatrainBot(discord.Client):
    """`watcher_source` and `web_source` are two SncfClients (consumers "watcher" and "web")."""

    def __init__(
        self,
        repo: Repo,
        *,
        watcher_source,
        web_source,
        client_id: str | None = None,
        dev_guild_id: int | None = None,
        clock: Callable[[], datetime] = now_paris,
    ) -> None:
        super().__init__(intents=discord.Intents.none())
        self.repo = repo
        self.web_source = web_source
        self.limiter = SlidingWindowLimiter(USER_RATE_LIMIT, USER_RATE_WINDOW_S)
        self.notifier = DiscordNotifier(self)
        self.watcher = Watcher(repo, watcher_source, self.notifier)
        self._client_id = client_id
        self._dev_guild_id = dev_guild_id
        self._clock = clock
        self._watcher_task: asyncio.Task | None = None
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self._on_command_error
        self._register_commands()

    # --- lifecycle -----------------------------------------------------------------------------

    async def setup_hook(self) -> None:
        self.add_dynamic_items(ReplaceButton, KeepButton)
        if self._dev_guild_id is not None:
            guild = discord.Object(id=self._dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        log.info("Commandes synchronisées")
        self._watcher_task = asyncio.create_task(self.watcher.run_forever(), name="watcher")

    async def close(self) -> None:
        if self._watcher_task is not None:
            self._watcher_task.cancel()
            await asyncio.gather(self._watcher_task, return_exceptions=True)
            self._watcher_task = None
        await super().close()

    # --- shared by commands and buttons --------------------------------------------------------

    @property
    def install_url(self) -> str:
        return INSTALL_URL.format(client_id=self._client_id or self.application_id)

    async def replace_follow(self, user_id: str, follow_id: int, hhmm: str) -> str:
        return await handle_remplacer(
            self.repo, self.web_source, self.limiter, user_id, follow_id, hhmm, self._clock()
        )

    async def _welcome(self, user_id: str, reply: str) -> str:
        """First follow of a user: check that DMs get through, remember it, and warn if not."""
        sent = await self.notifier.send(user_id, WELCOME_DM)
        self.repo.set_dm_ok(user_id, sent)
        warning = "\n" + dm_warning(self.install_url)
        if sent:
            return reply.replace(warning, "")
        return reply if warning in reply else reply + warning

    def _allowed(self, interaction: discord.Interaction) -> bool:
        return self.limiter.hit(str(interaction.user.id))

    @staticmethod
    async def _reply(interaction: discord.Interaction, text: str) -> None:
        await interaction.followup.send(text, ephemeral=True)

    async def _on_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.error("Commande en échec : %s", interaction.command and interaction.command.name, exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(MSG_ERROR, ephemeral=True)
            else:
                await interaction.response.send_message(MSG_ERROR, ephemeral=True)
        except discord.HTTPException:
            log.warning("Message d'erreur non envoyé", exc_info=True)

    # --- commands ------------------------------------------------------------------------------

    def _register_commands(self) -> None:
        tree = self.tree
        everywhere = (
            app_commands.allowed_installs(guilds=True, users=True),
            app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True),
        )

        async def gare_choices(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
            return _choices(autocomplete_gare(current))

        @tree.command(name="suivre", description="Suivre un train : je te préviens en cas de retard")
        @app_commands.describe(
            depart="Gare de départ",
            arrivee="Gare d'arrivée",
            heure="Heure de départ (HH:MM)",
            jours="Jours de circulation",
            jours_perso="Avec « perso » : des jours comme lun,mer,ven",
        )
        @app_commands.choices(jours=JOURS)
        @app_commands.autocomplete(depart=gare_choices, arrivee=gare_choices)
        async def suivre(
            interaction: discord.Interaction,
            depart: str,
            arrivee: str,
            heure: str,
            jours: str = "semaine",
            jours_perso: str | None = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True)  # resolving the train calls the SNCF API
            user_id = str(interaction.user.id)
            first_follow = not self.repo.list_follows(user_id)
            reply = await handle_suivre(
                self.repo,
                self.web_source,
                self.limiter,
                user_id,
                interaction.user.name,
                depart,
                arrivee,
                heure,
                jours,
                jours_perso,
                self._clock(),
                install_url=self.install_url,
            )
            if first_follow and reply.startswith("✅") and self.repo.list_follows(user_id):
                reply = await self._welcome(user_id, reply)
            await self._reply(interaction, reply)

        @suivre.autocomplete("heure")
        async def suivre_heure(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
            if not self._allowed(interaction):
                return []
            namespace = interaction.namespace
            pairs = await autocomplete_heure(
                self.web_source,
                getattr(namespace, "depart", None),
                getattr(namespace, "arrivee", None),
                current,
                self._clock(),
            )
            return _choices(pairs)

        @tree.command(name="mes-trains", description="Voir les trains que tu suis")
        async def mes_trains(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            reply = await handle_mes_trains(
                self.repo, self.web_source, self.limiter, str(interaction.user.id), self._clock()
            )
            await self._reply(interaction, reply)

        @tree.command(name="retirer", description="Ne plus suivre un train")
        @app_commands.describe(train="Le train à retirer")
        async def retirer(interaction: discord.Interaction, train: str) -> None:
            user_id = str(interaction.user.id)
            reply = handle_retirer(self.repo, user_id, int(train)) if train.isdigit() else MSG_NOT_YOURS
            await interaction.response.send_message(reply, ephemeral=True)

        @retirer.autocomplete("train")
        async def retirer_train(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
            return _choices(autocomplete_mes_trains(self.repo, str(interaction.user.id)))

        @tree.command(name="departs", description="Les prochains départs d'une gare")
        @app_commands.describe(gare="Gare de départ", vers="Gare d'arrivée (facultatif)")
        @app_commands.autocomplete(gare=gare_choices, vers=gare_choices)
        async def departs(interaction: discord.Interaction, gare: str, vers: str | None = None) -> None:
            if not self._allowed(interaction):
                await interaction.response.send_message(MSG_RATE, ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            reply = await handle_departs(self.repo, self.web_source, self.limiter, gare, vers, self._clock())
            await self._reply(interaction, reply)

        for command in (suivre, mes_trains, retirer, departs):
            for decorate in everywhere:
                decorate(command)
