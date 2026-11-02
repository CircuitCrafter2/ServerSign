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

try:  # Sign API: absent from published Endstone versions (<= 0.11.12), present on develop branch
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

LINE_COUNT = 4  # sign text lines (full mode only)
COMMAND_SLOTS = 3  # command fields in the form

NO_PERM = "§cYou do not have permission to do this."


def text_fields() -> int:
    """Number of text fields in the form: 0 if the sign API is unavailable."""
    return LINE_COUNT if HAS_SIGN_API else 0


def parse_form_data(raw) -> list | None:
    """ModalForm responses arrive as a JSON array (string or list depending on the version)."""
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
            "description": "Manages ServerSign signs.",
            "usages": ["/serversign (create|cancel|list|reload)<action: ServerSignAction>"],
            "permissions": ["serversign.command.serversign"],
        }
    }

    permissions = {
        "serversign.command.serversign": {"description": "Use /serversign.", "default": "op"},
        PERM_CREATE: {"description": "Create and modify ServerSign signs.", "default": "op"},
        PERM_CONSOLE: {"description": "Create signs executed by the console.", "default": "op"},
        PERM_BREAK: {"description": "Break a ServerSign sign.", "default": "op"},
        PERM_USE: {"description": "Click a ServerSign sign.", "default": True},
    }

    # ------------------------------------------------------------------ lifecycle

    def on_enable(self) -> None:
        self._creating: set = set()
        self._last_use: dict = {}
        try:
            self.save_default_config()
        except Exception as exc:  # noqa: BLE001 - default config is not critical
            self.logger.warning(f"Default config not copied: {exc}")
        self._load_config()
        self.data_folder.mkdir(parents=True, exist_ok=True)
        self.store = SignStore(self.data_folder / "signs.json")
        warning = self.store.load()
        if warning:
            self.logger.error(warning)
        self.logger.info(f"{len(self.store)} ServerSign sign(s) loaded.")
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

    # ------------------------------------------------------------------ utilities

    @staticmethod
    def _pos(block) -> tuple:
        """Position as pure Python values.

        IMPORTANT: never keep a native Block (or Player) object beyond the event that provided it:
        it is a raw pointer, and using it later will crash the server (SIGSEGV).
        """
        dim = block.dimension
        # Endstone develop renamed Dimension.name to Dimension.id
        name = getattr(dim, "id", None) or dim.name
        return (str(name), int(block.x), int(block.y), int(block.z))

    @staticmethod
    def _key(block) -> str:
        return make_key(*ServerSignPlugin._pos(block))

    @staticmethod
    def _fresh_block(player: Player, pos: tuple):
        """Rereads the block fresh from the world, at the moment it is needed."""
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
            self.logger.error(f"Failed to save signs.json: {exc}")
            return False

    def _later(self, player: Player, fn) -> None:
        """Executes fn(player) on the next tick, with the player retrieved by their ID (never the old object)."""
        uid = player.unique_id

        def task() -> None:
            fresh = self.server.get_player(uid)
            if fresh is not None:
                fn(fresh)

        try:
            self.server.scheduler.run_task(self, task, delay=1)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"Scheduler unavailable ({exc}), task abandoned.")

    def _creation_active(self, player: Player) -> bool:
        if not player.has_permission(PERM_CREATE):
            return False
        # Without the sign API, "always" would open the form on every click on a sign: ignored.
        return player.unique_id in self._creating or (self._always and HAS_SIGN_API)

    def _cooling_down(self, player: Player, key: str) -> bool:
        now = time.monotonic()
        per_player = self._last_use.setdefault(player.unique_id, {})
        last = per_player.get(key)
        if last is not None and (now - last) * 1000 < self._cooldown_ms:
            return True
        per_player[key] = now
        return False

    # ------------------------------------------------------------------ events

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
                self.store.remove(key)  # new placement over an outdated entry
                self._persist()
                existing = None

            if self._creation_active(player):
                event.cancel()  # replaces vanilla editor with our form
                side = event.side
                self._later(player, lambda p: self._open_form(p, pos, side, self.store.get(key)))
            elif existing is not None:
                event.cancel()  # a ServerSign sign cannot be edited manually

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
            # With the sign API, the form opens via PlayerOpenSignEvent.
            # Without it, clicking the sign opens it (commands + executor only).
            if not HAS_SIGN_API and (entry is not None or self._is_sign(block)):
                event.cancel()
                self._open_form(player, pos, None, entry)  # direct: no native object kept
            return

        if entry is None:
            return
        event.cancel()  # prevents placing a block against the sign / opening editor
        if not player.has_permission(PERM_USE):
            player.send_message(NO_PERM)
            return
        if self._cooling_down(player, key):
            return
        self._run(entry, player)

    @event_handler
    def on_place(self, event: BlockPlaceEvent) -> None:
        # A block placed where an old entry lingered (sign destroyed without event): purge.
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
            event.player.send_message("§eServerSign removed.")
        else:
            event.cancel()
            event.player.send_message("§cThis sign is protected.")

    @event_handler
    def on_quit(self, event: PlayerQuitEvent) -> None:
        uid = event.player.unique_id
        self._creating.discard(uid)
        self._last_use.pop(uid, None)

    # ------------------------------------------------------------------ execution

    def _run(self, entry: SignEntry, player: Player) -> None:
        sender = self.server.command_sender if entry.executor == EXECUTOR_CONSOLE else player
        for template in entry.commands:
            command = render_command(template, player.name)
            try:
                if not self.server.dispatch_command(sender, command):
                    self.logger.warning(f"Command had no effect for {player.name}: /{command}")
            except Exception as exc:  # noqa: BLE001 - a failed command must not block subsequent ones
                self.logger.error(f"Error on /{command}: {exc}")

    # ------------------------------------------------------------------ form

    def _open_form(self, player: Player, pos: tuple, side, existing: SignEntry | None) -> None:
        can_console = player.has_permission(PERM_CONSOLE)
        if existing is not None and existing.executor == EXECUTOR_CONSOLE and not can_console:
            player.send_message("§cThis sign is executed by the console: you cannot modify it.")
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
                controls.append(TextInput(f"Line {i + 1}", "", current[i] if i < len(current) else ""))
        commands = existing.commands if existing else []
        for i in range(COMMAND_SLOTS):
            value = commands[i] if i < len(commands) else ""
            controls.append(TextInput(f"Command {i + 1}", "ex: tp {player} 0 70 0", value))
        executors = ["Player"] + (["Console"] if can_console else [])
        default = 1 if (existing and existing.executor == EXECUTOR_CONSOLE and can_console) else 0
        controls.append(Dropdown("Executed by", executors, default))
        if existing is not None:
            controls.append(Toggle("Delete this ServerSign", False))

        form = ModalForm(
            title="ServerSign",
            controls=controls,
            submit_button="Save",
            on_submit=lambda p, raw: self._on_submit(p, pos, side, raw),
            on_close=lambda p: None,
        )
        player.send_form(form)

    def _on_submit(self, player: Player, pos: tuple, side, raw) -> None:
        try:
            self._handle_submit(player, pos, side, raw)
        except Exception as exc:  # noqa: BLE001 - never let an exception bubble up to the server
            self.logger.error(f"Error in ServerSign form: {exc}")
            player.send_message("§cInternal error, see console.")

    def _handle_submit(self, player: Player, pos: tuple, side, raw) -> None:
        n_text = text_fields()
        idx_cmd0, idx_exec = n_text, n_text + COMMAND_SLOTS
        idx_delete = idx_exec + 1
        data = parse_form_data(raw)
        if data is None or len(data) <= idx_exec:
            player.send_message("§cInvalid form response.")
            return

        # Permissions re-verified here: rights changed since opening, or response forged by a modified client.
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
                player.send_message("§eServerSign removed.")
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
            player.send_message(f"§c{rejected} command(s) ignored (forbidden control characters).")

        if n_text:
            block = self._fresh_block(player, pos)
            state = block.capture_state() if block is not None else None
            if not isinstance(state, Sign):
                player.send_message("§cThis block is no longer a sign.")
                return
            sign_side = state.get_side(side)
            for i in range(n_text):
                sign_side.set_line(i, str(data[i] or ""))
            if not state.update():
                player.send_message("§cFailed to modify sign text.")
                return

        if commands:
            self.store.set(key, SignEntry(commands, executor, player.name))
            ok = self._persist()
            player.send_message(
                f"§aServerSign saved ({len(commands)} command(s), execution: "
                f"{'console' if executor == EXECUTOR_CONSOLE else 'player'})."
                if ok
                else "§cServerSign active but not saved (see console)."
            )
        else:
            if self.store.remove(key):
                self._persist()
            player.send_message("§eNo valid command: the sign is not a ServerSign.")

    # ------------------------------------------------------------------ /serversign command

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name != "serversign":
            return False
        action = args[0].lower() if args else ""

        if action in ("create", "cancel"):
            if not isinstance(sender, Player):
                sender.send_message("§cThis action is restricted to players.")
                return True
            if not sender.has_permission(PERM_CREATE):
                sender.send_message(NO_PERM)
                return True
            if action == "create":
                self._creating.add(sender.unique_id)
                sender.send_message("§aCreation mode enabled: " + ("place or touch a sign." if HAS_SIGN_API else "right-click a sign."))
            else:
                self._creating.discard(sender.unique_id)
                sender.send_message("§eCreation mode disabled.")
        elif action == "list":
            items = self.store.items()
            sender.send_message(f"§e{len(items)} ServerSign(s):")
            for key, entry in items[:20]:
                sender.send_message(f"§7- {key} [{entry.executor}] {len(entry.commands)} cmd")
            if len(items) > 20:
                sender.send_message(f"§7... and {len(items) - 20} other(s).")
        elif action == "reload":
            self._load_config()
            warning = self.store.load()
            sender.send_message(f"§c{warning}" if warning else f"§aReloaded: {len(self.store)} sign(s).")
        else:
            sender.send_message("§eUsage: /serversign (create|cancel|list|reload)")
        return True