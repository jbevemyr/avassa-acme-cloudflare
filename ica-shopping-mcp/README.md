# ICA Shopping List MCP Server

An MCP (Model Context Protocol) server that lets an AI assistant manage your
[ICA](https://www.ica.se) shopping lists — most importantly, **add items** to
them. Authentication uses your ICA username (personnummer) and password.

## How it works

ICA has no public API. This server talks to the same backend as the ICA
mobile app:

1. **Authentication** against `ims.icagruppen.se` (Curity Identity Server)
   using the app's OAuth2 flow: dynamic client registration, then an
   authorization-code grant with PKCE where your username/password are posted
   to the HTML-form authenticator.
2. **Shopping list operations** against
   `apimgw-pub.ica.se/sverige/digx/mobile/shoppinglistservice/v1/...`.

Tokens (and the registered OAuth client) are persisted to a state file
(`~/.config/ica-mcp/auth-state.json` by default, mode 0600), so the full
login only runs once; afterwards the short-lived access token is refreshed
automatically via the refresh token.

The authentication flow is a standalone port of the one reverse engineered by
the [LazyTarget/ha-ica-todo](https://github.com/LazyTarget/ha-ica-todo)
Home Assistant integration — credit to that project.

## Caveats

- **Swedish IP required.** ICA's API gateway returns HTTP 451 to non-Swedish
  IP addresses. Run the server on a machine/network in Sweden.
- **Password login required.** The account must be able to log in with
  personnummer + password. Accounts locked to BankID-only will not work.
- This is an **unofficial** API and may change or break at any time.

## Installation

```bash
cd ica-shopping-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configuration

The server is configured through environment variables:

| Variable         | Required | Description                                        |
|------------------|----------|----------------------------------------------------|
| `ICA_USERNAME`   | yes      | Your ICA username (personnummer)                   |
| `ICA_PASSWORD`   | yes      | Your ICA password                                  |
| `ICA_STATE_FILE` | no       | Token cache path (default `~/.config/ica-mcp/auth-state.json`) |

### Claude Code

```bash
claude mcp add ica-shopping \
  -e ICA_USERNAME=YYYYMMDDNNNN \
  -e ICA_PASSWORD=your-password \
  -- /path/to/ica-shopping-mcp/.venv/bin/python /path/to/ica-shopping-mcp/server.py
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "ica-shopping": {
      "command": "/path/to/ica-shopping-mcp/.venv/bin/python",
      "args": ["/path/to/ica-shopping-mcp/server.py"],
      "env": {
        "ICA_USERNAME": "YYYYMMDDNNNN",
        "ICA_PASSWORD": "your-password"
      }
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `list_shopping_lists` | List all shopping lists (title, id, item count) |
| `get_shopping_list` | Get a list with all its items |
| `add_items_to_shopping_list` | Add items (name + optional quantity/unit) to a list |
| `remove_items_from_shopping_list` | Remove items by name |
| `set_items_checked` | Mark items as bought / not bought |
| `create_shopping_list` | Create a new list |

Every tool that operates on a list takes an optional `list_name` (title or
offlineId). If omitted, the account's first shopping list is used.

Example prompt once connected:

> Add 2 l mjölk, smör and bananer to my ICA shopping list.

## Testing from the command line

A quick smoke test without an MCP client:

```bash
ICA_USERNAME=... ICA_PASSWORD=... .venv/bin/python -c "
from ica_client import IcaClient
import os
c = IcaClient(os.environ['ICA_USERNAME'], os.environ['ICA_PASSWORD'],
              os.path.expanduser('~/.config/ica-mcp/auth-state.json'))
for l in c.get_shopping_lists():
    print(l['title'], l['offlineId'])
"
```
