# endstone-serversign

Clickable signs that execute commands, for Endstone.

## Usage
1. Place a sign and write its text normally (vanilla editor).
2. Type `/serversign create`, then right-click the sign: a form will ask for the commands and the executor (player / console).
3. Type `/serversign cancel`. From now on, right-clicking the sign executes the commands.

The form replaces the vanilla editor and also asks for the 4 lines of text.

`{player}` is replaced by the player's username. To modify a ServerSign: `/serversign create` then right-click it.

## Permissions
| Permission | Default | Role |
|---|---|---|
| `serversign.command.serversign` | op | Use `/serversign` |
| `serversign.create` | op | Create / modify |
| `serversign.console` | op | Choose "Console" as the executor |
| `serversign.break` | op | Break a ServerSign |
| `serversign.use` | everyone | Click on a sign |

## Installation
Run `pip install build && python -m build`, then copy the `.whl` file from `dist/` into the `plugins/` folder.