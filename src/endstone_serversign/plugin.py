import json
import time

from endstone import Player
from endstone.command import Command, CommandSender
from endstone.event import (
    BlockBreakEvent,
    BlockPlaceEvent,
    PlayerInteractEvent,
    PlayerQuitEvent,
    event_handler,
)
from endstone.form import Dropdown, ModalForm, TextInput, Toggle
from endstone.plugin import Plugin

try:  # API des panneaux : absente des versions publiées d'Endstone (<= 0.11.12), présente sur la branche develop
    from endstone.block import Sign
    from endstone.event import PlayerOpenSignEvent

    HAS_SIGN_API = True
except ImportError:
    Sign = None
    PlayerOpenSignEvent = None
    HAS_SIGN_API = False

from .logic import (
    EXECUTOR_CONSOLE,
    EXECUTOR_PLAYER,
    SignEntry,
    SignStore,
    clean_commands,
    make_key,
    render_command,
)

PERM_USE = "serversign.use"
PERM_CREATE = "serversign.create"
PERM_CONSOLE = "serversign.console"
PERM_BREAK = "serversign.break"

LINE_COUNT = 4  # lignes de texte d'un panneau (mode complet uniquement)
COMMAND_SLOTS = 3  # champs de commande dans le formulaire

NO_PERM = "§cVous n'avez pas la permission de faire cela."


def text_fields() -> int:
    """Nombre de champs de texte dans le formulaire : 0 si l'API des panneaux est indisponible."""
    return LINE_COUNT if HAS_SIGN_API else 0


def parse_form_data(raw) -> list | None:
    """Les réponses d'un ModalForm arrivent sous forme de tableau JSON (chaîne ou liste selon la version)."""
    try:
        if isinstance(raw, (str, bytes, bytearray)):
            data = json.loads(raw)
        else:
            try:
                data = list(raw)
            except TypeError:
                data = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, list) else None


class ServerSignPlugin(Plugin):
    prefix = "ServerSign"
    api_version = "0.11"

    commands = {
        "serversign": {
            "description": "Gère les panneaux ServerSign.",
            "usages": ["/serversign (create|cancel|list|reload)<action: ServerSignAction>"],
            "permissions": ["serversign.command.serversign"],
        }
    }

    permissions = {
        "serversign.command.serversign": {"description": "Utiliser /serversign.", "default": "op"},
        PERM_CREATE: {"description": "Créer et modifier des panneaux ServerSign.", "default": "op"},
        PERM_CONSOLE: {"description": "Créer des panneaux exécutés par la console.", "default": "op"},
        PERM_BREAK: {"description": "Casser un panneau ServerSign.", "default": "op"},
        PERM_USE: {"description": "Cliquer sur un panneau ServerSign.", "default": True},
    }

    # ------------------------------------------------------------------ cycle de vie

    def on_enable(self) -> None:
        self._creating: set = set()
        self._last_use: dict = {}
        try:
            self.save_default_config()
        except Exception as exc:  # noqa: BLE001 - la config par défaut n'est pas critique
            self.logger.warning(f"Config par défaut non copiée : {exc}")
        self._load_config()
        self.data_folder.mkdir(parents=True, exist_ok=True)
        self.store = SignStore(self.data_folder / "signs.json")
        warning = self.store.load()
        if warning:
            self.logger.error(warning)
        self.logger.info(f"{len(self.store)} panneau(x) ServerSign chargé(s).")
        self.register_events(self)

    def _load_config(self) -> None:
        try:
            cfg = dict(self.config)
        except Exception:  # noqa: BLE001
            cfg = {}
        self._always = str(cfg.get("creation_mode", "toggle")).lower() == "always"
        try:
            self._cooldown_ms = max(0, int(cfg.get("cooldown_ms", 1000)))
        except (TypeError, ValueError):
            self._cooldown_ms = 1000

    # ------------------------------------------------------------------ utilitaires

    @staticmethod
    def _pos(block) -> tuple:
        """Position sous forme de valeurs Python pures.

        IMPORTANT : ne jamais conserver un objet Block (ni Player) natif au-delà de l'événement qui l'a fourni :
        c'est un pointeur brut, et l'utiliser plus tard fait planter le serveur (SIGSEGV).
        """
        dim = block.dimension
        # Endstone develop a renommé Dimension.name en Dimension.id
        name = getattr(dim, "id", None) or dim.name
        return (str(name), int(block.x), int(block.y), int(block.z))

    @staticmethod
    def _key(block) -> str:
        return make_key(*ServerSignPlugin._pos(block))

    @staticmethod
    def _fresh_block(player: Player, pos: tuple):
        """Relit le bloc à neuf depuis le monde, au moment où on en a besoin."""
        try:
            return player.dimension.get_block_at(pos[1], pos[2], pos[3])
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _is_sign(block) -> bool:
        return "sign" in str(block.type).lower()

    def _persist(self) -> bool:
        try:
            self.store.save()
            return True
        except OSError as exc:
            self.logger.error(f"Impossible d'enregistrer signs.json : {exc}")
            return False

    def _later(self, player: Player, fn) -> None:
        """Exécute fn(player) au tick suivant, avec le joueur retrouvé par son identifiant (jamais l'ancien objet)."""
        uid = player.unique_id

        def task() -> None:
            fresh = self.server.get_player(uid)
            if fresh is not None:
                fn(fresh)

        try:
            self.server.scheduler.run_task(self, task, delay=1)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"Scheduler indisponible ({exc}), tâche abandonnée.")

    def _creation_active(self, player: Player) -> bool:
        if not player.has_permission(PERM_CREATE):
            return False
        # Sans l'API des panneaux, "always" ouvrirait le formulaire à chaque clic sur un panneau : ignoré.
        return player.unique_id in self._creating or (self._always and HAS_SIGN_API)

    def _cooling_down(self, player: Player, key: str) -> bool:
        now = time.monotonic()
        per_player = self._last_use.setdefault(player.unique_id, {})
        last = per_player.get(key)
        if last is not None and (now - last) * 1000 < self._cooldown_ms:
            return True
        per_player[key] = now
        return False

    # ------------------------------------------------------------------ événements

    if HAS_SIGN_API:

        @event_handler
        def on_open_sign(self, event: PlayerOpenSignEvent) -> None:
            if event.cause == PlayerOpenSignEvent.Cause.PLUGIN:
                return
            player = event.player
            pos = self._pos(event.sign.block)
            key = make_key(*pos)
            existing = self.store.get(key)

            if existing is not None and event.cause == PlayerOpenSignEvent.Cause.PLACE:
                self.store.remove(key)  # nouvelle pose sur une entrée périmée
                self._persist()
                existing = None

            if self._creation_active(player):
                event.cancel()  # remplace l'éditeur vanilla par notre formulaire
                side = event.side
                self._later(player, lambda p: self._open_form(p, pos, side, self.store.get(key)))
            elif existing is not None:
                event.cancel()  # un panneau ServerSign ne s'édite pas à la main

    @event_handler
    def on_interact(self, event: PlayerInteractEvent) -> None:
        if event.action != PlayerInteractEvent.Action.RIGHT_CLICK_BLOCK or not event.has_block:
            return
        block = event.block
        pos = self._pos(block)
        key = make_key(*pos)
        entry = self.store.get(key)
        player = event.player

        if self._creation_active(player):
            # Avec l'API des panneaux, le formulaire s'ouvre via PlayerOpenSignEvent.
            # Sans elle, c'est le clic sur le panneau qui l'ouvre (commandes + exécutant uniquement).
            if not HAS_SIGN_API and (entry is not None or self._is_sign(block)):
                event.cancel()
                self._open_form(player, pos, None, entry)  # direct : aucun objet natif conservé
            return

        if entry is None:
            return
        event.cancel()  # évite de poser un bloc contre le panneau / d'ouvrir l'éditeur
        if not player.has_permission(PERM_USE):
            player.send_message(NO_PERM)
            return
        if self._cooling_down(player, key):
            return
        self._run(entry, player)

    @event_handler
    def on_place(self, event: BlockPlaceEvent) -> None:
        # Un bloc posé là où une ancienne entrée traînait (panneau détruit sans événement) : on purge.
        key = self._key(event.block)
        if key in self.store:
            self.store.remove(key)
            self._persist()

    @event_handler
    def on_break(self, event: BlockBreakEvent) -> None:
        key = self._key(event.block)
        if key not in self.store:
            return
        if event.player.has_permission(PERM_BREAK):
            self.store.remove(key)
            self._persist()
            event.player.send_message("§eServerSign supprimé.")
        else:
            event.cancel()
            event.player.send_message("§cCe panneau est protégé.")

    @event_handler
    def on_quit(self, event: PlayerQuitEvent) -> None:
        uid = event.player.unique_id
        self._creating.discard(uid)
        self._last_use.pop(uid, None)

    # ------------------------------------------------------------------ exécution

    def _run(self, entry: SignEntry, player: Player) -> None:
        sender = self.server.command_sender if entry.executor == EXECUTOR_CONSOLE else player
        for template in entry.commands:
            command = render_command(template, player.name)
            try:
                if not self.server.dispatch_command(sender, command):
                    self.logger.warning(f"Commande sans effet pour {player.name} : /{command}")
            except Exception as exc:  # noqa: BLE001 - une commande ratée ne doit pas bloquer les suivantes
                self.logger.error(f"Erreur sur /{command} : {exc}")

    # ------------------------------------------------------------------ formulaire

    def _open_form(self, player: Player, pos: tuple, side, existing: SignEntry | None) -> None:
        can_console = player.has_permission(PERM_CONSOLE)
        if existing is not None and existing.executor == EXECUTOR_CONSOLE and not can_console:
            player.send_message("§cCe panneau est exécuté par la console : vous ne pouvez pas le modifier.")
            return
        n_text = text_fields()
        controls: list = []
        if n_text:
            block = self._fresh_block(player, pos)
            state = block.capture_state() if block is not None else None
            if not isinstance(state, Sign):
                return
            current = list(state.get_side(side).lines)
            for i in range(n_text):
                controls.append(TextInput(f"Ligne {i + 1}", "", current[i] if i < len(current) else ""))
        commands = existing.commands if existing else []
        for i in range(COMMAND_SLOTS):
            value = commands[i] if i < len(commands) else ""
            controls.append(TextInput(f"Commande {i + 1}", "ex : tp {player} 0 70 0", value))
        executors = ["Joueur"] + (["Console"] if can_console else [])
        default = 1 if (existing and existing.executor == EXECUTOR_CONSOLE and can_console) else 0
        controls.append(Dropdown("Exécutée par", executors, default))
        if existing is not None:
            controls.append(Toggle("Supprimer ce ServerSign", False))

        form = ModalForm(
            title="ServerSign",
            controls=controls,
            submit_button="Enregistrer",
            on_submit=lambda p, raw: self._on_submit(p, pos, side, raw),
            on_close=lambda p: None,
        )
        player.send_form(form)

    def _on_submit(self, player: Player, pos: tuple, side, raw) -> None:
        try:
            self._handle_submit(player, pos, side, raw)
        except Exception as exc:  # noqa: BLE001 - ne jamais laisser une exception remonter dans le serveur
            self.logger.error(f"Erreur dans le formulaire ServerSign : {exc}")
            player.send_message("§cErreur interne, voir la console.")

    def _handle_submit(self, player: Player, pos: tuple, side, raw) -> None:
        n_text = text_fields()
        idx_cmd0, idx_exec = n_text, n_text + COMMAND_SLOTS
        idx_delete = idx_exec + 1
        data = parse_form_data(raw)
        if data is None or len(data) <= idx_exec:
            player.send_message("§cRéponse du formulaire invalide.")
            return

        # Permissions revérifiées ici : droits changés depuis l'ouverture, ou réponse forgée par un client modifié.
        if not player.has_permission(PERM_CREATE):
            player.send_message(NO_PERM)
            return
        can_console = player.has_permission(PERM_CONSOLE)
        key = make_key(*pos)
        existing = self.store.get(key)
        if existing is not None and existing.executor == EXECUTOR_CONSOLE and not can_console:
            player.send_message(NO_PERM)
            return

        if len(data) > idx_delete and data[idx_delete] is True:
            if existing is not None and self.store.remove(key) and self._persist():
                player.send_message("§eServerSign supprimé.")
            return

        executor = EXECUTOR_PLAYER
        choice = data[idx_exec]
        if isinstance(choice, int) and not isinstance(choice, bool) and choice == 1:
            if not can_console:
                player.send_message(NO_PERM)
                return
            executor = EXECUTOR_CONSOLE

        commands, rejected = clean_commands(data[idx_cmd0:idx_exec], COMMAND_SLOTS)
        if rejected:
            player.send_message(f"§c{rejected} commande(s) ignorée(s) (caractères de contrôle interdits).")

        if n_text:
            block = self._fresh_block(player, pos)
            state = block.capture_state() if block is not None else None
            if not isinstance(state, Sign):
                player.send_message("§cCe bloc n'est plus un panneau.")
                return
            sign_side = state.get_side(side)
            for i in range(n_text):
                sign_side.set_line(i, str(data[i] or ""))
            if not state.update():
                player.send_message("§cImpossible de modifier le texte du panneau.")
                return

        if commands:
            self.store.set(key, SignEntry(commands, executor, player.name))
            ok = self._persist()
            player.send_message(
                f"§aServerSign enregistré ({len(commands)} commande(s), exécution : "
                f"{'console' if executor == EXECUTOR_CONSOLE else 'joueur'})."
                if ok
                else "§cServerSign actif mais non sauvegardé (voir console)."
            )
        else:
            if self.store.remove(key):
                self._persist()
            player.send_message("§eAucune commande valide : le panneau n'est pas un ServerSign.")

    # ------------------------------------------------------------------ commande /serversign

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name != "serversign":
            return False
        action = args[0].lower() if args else ""

        if action in ("create", "cancel"):
            if not isinstance(sender, Player):
                sender.send_message("§cCette action est réservée aux joueurs.")
                return True
            if not sender.has_permission(PERM_CREATE):
                sender.send_message(NO_PERM)
                return True
            if action == "create":
                self._creating.add(sender.unique_id)
                sender.send_message("§aMode création activé : " + ("posez ou touchez un panneau." if HAS_SIGN_API else "faites un clic droit sur un panneau."))
            else:
                self._creating.discard(sender.unique_id)
                sender.send_message("§eMode création désactivé.")
        elif action == "list":
            items = self.store.items()
            sender.send_message(f"§e{len(items)} ServerSign :")
            for key, entry in items[:20]:
                sender.send_message(f"§7- {key} [{entry.executor}] {len(entry.commands)} cmd")
            if len(items) > 20:
                sender.send_message(f"§7... et {len(items) - 20} autre(s).")
        elif action == "reload":
            self._load_config()
            warning = self.store.load()
            sender.send_message(f"§c{warning}" if warning else f"§aRechargé : {len(self.store)} panneau(x).")
        else:
            sender.send_message("§eUsage : /serversign (create|cancel|list|reload)")
        return True
