"""MCP server exposing ICA shopping lists as tools.

Runs over stdio. Configuration via environment variables:

  ICA_USERNAME    ICA username (personnummer)
  ICA_PASSWORD    ICA password
  ICA_STATE_FILE  Where to persist OAuth tokens
                  (default: ~/.config/ica-mcp/auth-state.json)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from ica_client import IcaClient, IcaError

logging.basicConfig(level=logging.INFO)

mcp = FastMCP("ica-shopping")

_client: IcaClient | None = None


def get_client() -> IcaClient:
    global _client
    if _client is None:
        username = os.environ.get("ICA_USERNAME")
        password = os.environ.get("ICA_PASSWORD")
        if not username or not password:
            raise IcaError(
                "Set the ICA_USERNAME and ICA_PASSWORD environment variables "
                "in the MCP server configuration."
            )
        state_file = os.environ.get(
            "ICA_STATE_FILE",
            os.path.join(
                os.path.expanduser("~"), ".config", "ica-mcp", "auth-state.json"
            ),
        )
        _client = IcaClient(username, password, state_file)
    return _client


class ShoppingItem(BaseModel):
    """An item to put on a shopping list."""

    name: str = Field(description="Product name, e.g. 'Mjölk'")
    quantity: float | None = Field(
        default=None, description="Optional quantity, e.g. 2"
    )
    unit: str | None = Field(
        default=None, description="Optional unit, e.g. 'st', 'kg', 'l', 'förp'"
    )


def _resolve_list(list_name: str) -> dict[str, Any]:
    """Find a shopping list by title or offlineId; empty name = first list."""
    lists = get_client().get_shopping_lists()
    if not lists:
        raise IcaError(
            "No shopping lists exist on this account. "
            "Create one first with create_shopping_list."
        )
    if not list_name:
        return lists[0]
    wanted = list_name.casefold()
    for lst in lists:
        if lst.get("offlineId") == list_name or str(lst.get("id")) == list_name:
            return lst
    for lst in lists:
        if (lst.get("title") or "").casefold() == wanted:
            return lst
    for lst in lists:
        if wanted in (lst.get("title") or "").casefold():
            return lst
    titles = ", ".join(repr(lst.get("title")) for lst in lists)
    raise IcaError(f"No shopping list matching {list_name!r}. Available: {titles}")


def _summarize_list(lst: dict[str, Any], include_items: bool = True) -> dict[str, Any]:
    rows = lst.get("rows") or []
    summary: dict[str, Any] = {
        "title": lst.get("title"),
        "offlineId": lst.get("offlineId"),
        "itemCount": len(rows),
    }
    if include_items:
        summary["items"] = [
            {
                "name": row.get("productName"),
                "quantity": row.get("quantity"),
                "unit": row.get("unit"),
                "checked": bool(row.get("isStrikedOver")),
                "offlineId": row.get("offlineId"),
            }
            for row in rows
        ]
    return summary


@mcp.tool()
def list_shopping_lists() -> str:
    """List all ICA shopping lists on the account (titles, ids and item counts)."""
    lists = get_client().get_shopping_lists()
    return json.dumps(
        [_summarize_list(lst, include_items=False) for lst in lists],
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def get_shopping_list(list_name: str = "") -> str:
    """Get a shopping list with all its items.

    Args:
        list_name: Title (or offlineId) of the list. Leave empty to use the
            account's first list.
    """
    lst = _resolve_list(list_name)
    full = get_client().get_shopping_list(lst["offlineId"])
    return json.dumps(_summarize_list(full), ensure_ascii=False, indent=2)


@mcp.tool()
def add_items_to_shopping_list(
    items: list[ShoppingItem], list_name: str = ""
) -> str:
    """Add one or more items to an ICA shopping list.

    Args:
        items: Items to add. Each has a name and optional quantity/unit,
            e.g. [{"name": "Mjölk", "quantity": 2, "unit": "l"}, {"name": "Smör"}].
        list_name: Title (or offlineId) of the list. Leave empty to use the
            account's first list.
    """
    if not items:
        raise IcaError("No items given")
    lst = _resolve_list(list_name)
    client = get_client()
    client.add_items(
        lst["offlineId"], [item.model_dump() for item in items]
    )
    added = ", ".join(item.name for item in items)
    full = client.get_shopping_list(lst["offlineId"])
    return (
        f"Added {len(items)} item(s) ({added}) to list '{lst.get('title')}'. "
        f"The list now has {len(full.get('rows') or [])} items."
    )


@mcp.tool()
def remove_items_from_shopping_list(
    item_names: list[str], list_name: str = ""
) -> str:
    """Remove items from an ICA shopping list by name.

    Args:
        item_names: Names of the items to remove (case-insensitive match).
        list_name: Title (or offlineId) of the list. Leave empty to use the
            account's first list.
    """
    lst = _resolve_list(list_name)
    client = get_client()
    full = client.get_shopping_list(lst["offlineId"])
    wanted = {name.casefold() for name in item_names}
    to_delete = [
        row["offlineId"]
        for row in full.get("rows") or []
        if (row.get("productName") or "").casefold() in wanted
    ]
    if not to_delete:
        return f"No matching items found on list '{lst.get('title')}'."
    client.delete_items(lst["offlineId"], to_delete)
    return f"Removed {len(to_delete)} item(s) from list '{lst.get('title')}'."


@mcp.tool()
def set_items_checked(
    item_names: list[str], checked: bool = True, list_name: str = ""
) -> str:
    """Mark items on a shopping list as checked (bought) or unchecked.

    Args:
        item_names: Names of the items to update (case-insensitive match).
        checked: True to mark as bought/striked, False to un-mark.
        list_name: Title (or offlineId) of the list. Leave empty to use the
            account's first list.
    """
    lst = _resolve_list(list_name)
    client = get_client()
    full = client.get_shopping_list(lst["offlineId"])
    wanted = {name.casefold() for name in item_names}
    rows = [
        row
        for row in full.get("rows") or []
        if (row.get("productName") or "").casefold() in wanted
    ]
    if not rows:
        return f"No matching items found on list '{lst.get('title')}'."
    client.set_items_striked(lst["offlineId"], rows, checked)
    state = "checked" if checked else "unchecked"
    return f"Marked {len(rows)} item(s) as {state} on list '{lst.get('title')}'."


@mcp.tool()
def create_shopping_list(title: str) -> str:
    """Create a new ICA shopping list.

    Args:
        title: Name of the new list.
    """
    lst = get_client().create_shopping_list(title)
    return json.dumps(_summarize_list(lst), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
