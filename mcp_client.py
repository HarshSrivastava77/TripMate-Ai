import os
import shutil
import sys
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient

from config import (
    BASE_DIR,
    TAVILY_API_KEY,
    TAVILY_API_KEY_FALLBACK,
    AVIATION_STACK_API_KEY,
    AVIATION_STACK_API_KEY_FALLBACK,
    OPENWEATHER_API_KEY,
    OPENWEATHER_API_KEY_FALLBACK,
    invoke_llm,
)

WEATHER_SERVER_PATH = BASE_DIR / "custom_weather_mcp_server.py"
UVX_COMMAND = shutil.which("uvx") or "uvx"


def _require_env(name: str, value: str | None) -> str:
    """Return an environment value or raise a readable setup error."""

    if not value:
        raise RuntimeError(
            f"{name} is missing. "
            f"Add {name}=your_key to the project .env file."
        )

    return value


def _subprocess_env(**updates: str | None) -> dict[str, str]:
    """
    Preserve the current Windows/Conda environment and add MCP API keys.
    """

    env = os.environ.copy()

    for key, value in updates.items():
        if value:
            env[key] = value

    return env


def _build_mcp_client(
    tavily_key: str | None,
    aviation_key: str | None,
    openweather_key: str | None,
) -> MultiServerMCPClient:
    """Build an MCP client configured with a given set of API keys."""

    return MultiServerMCPClient(
        {
            "tavily": {
                "transport": "streamable_http",
                "url": (
                    "https://mcp.tavily.com/mcp/"
                    f"?tavilyApiKey={tavily_key or ''}"
                ),
            },

            "aviationstack": {
                "transport": "stdio",
                "command": UVX_COMMAND,
                "args": [
                    "aviationstack-mcp",
                ],
                "env": _subprocess_env(
                    AVIATION_STACK_API_KEY=aviation_key,
                ),
            },

            "weather": {
                "transport": "stdio",

                # Uses the Python executable from the active Conda environment.
                "command": sys.executable,

                # Uses the weather server inside the current project folder.
                "args": [
                    str(WEATHER_SERVER_PATH),
                ],

                "env": _subprocess_env(
                    OPENWEATHER_API_KEY=openweather_key,
                ),
            },
        }
    )


# =========================================================
# MCP clients - primary and, if any *_FALLBACK key is set, a second
# client built with the fallback keys for automatic retry.
# =========================================================

client = _build_mcp_client(
    TAVILY_API_KEY,
    AVIATION_STACK_API_KEY,
    OPENWEATHER_API_KEY,
)

_HAS_ANY_FALLBACK_KEY = any(
    [
        TAVILY_API_KEY_FALLBACK,
        AVIATION_STACK_API_KEY_FALLBACK,
        OPENWEATHER_API_KEY_FALLBACK,
    ]
)

# Falls back to the primary key for any service whose *_FALLBACK wasn't
# set, so only the services you actually configured a fallback for get
# retried - the rest behave exactly as before (one key, no retry).
_fallback_client = (
    _build_mcp_client(
        TAVILY_API_KEY_FALLBACK or TAVILY_API_KEY,
        AVIATION_STACK_API_KEY_FALLBACK or AVIATION_STACK_API_KEY,
        OPENWEATHER_API_KEY_FALLBACK or OPENWEATHER_API_KEY,
    )
    if _HAS_ANY_FALLBACK_KEY
    else None
)

# Which services actually have a distinct fallback key configured (used
# to decide whether a retry is worth attempting for that service).
_FALLBACK_KEY_SET = {
    "tavily": bool(TAVILY_API_KEY_FALLBACK),
    "aviationstack": bool(AVIATION_STACK_API_KEY_FALLBACK),
    "weather": bool(OPENWEATHER_API_KEY_FALLBACK),
}


async def _get_server_tool(
    server_name: str,
    tool_name: str,
    use_fallback: bool = False,
):
    """
    Load one tool from one MCP server.

    This prevents a broken weather or AviationStack server from
    crashing an unrelated Tavily request.
    """

    active_client = _fallback_client if use_fallback else client
    tavily_key = TAVILY_API_KEY_FALLBACK if use_fallback else TAVILY_API_KEY
    aviation_key = (
        AVIATION_STACK_API_KEY_FALLBACK if use_fallback else AVIATION_STACK_API_KEY
    )
    openweather_key = (
        OPENWEATHER_API_KEY_FALLBACK if use_fallback else OPENWEATHER_API_KEY
    )

    if server_name == "tavily":
        _require_env(
            "TAVILY_API_KEY_FALLBACK" if use_fallback else "TAVILY_API_KEY",
            tavily_key,
        )

    elif server_name == "aviationstack":
        _require_env(
            "AVIATION_STACK_API_KEY_FALLBACK"
            if use_fallback
            else "AVIATION_STACK_API_KEY",
            aviation_key,
        )

        if shutil.which("uvx") is None:
            raise RuntimeError(
                "uvx was not found. Install uv, reopen the terminal, "
                "activate the travel environment, and run "
                "`uvx --version`."
            )

    elif server_name == "weather":
        _require_env(
            "OPENWEATHER_API_KEY_FALLBACK" if use_fallback else "OPENWEATHER_API_KEY",
            openweather_key,
        )

        if not WEATHER_SERVER_PATH.is_file():
            raise FileNotFoundError(
                f"Weather MCP server not found: "
                f"{WEATHER_SERVER_PATH}"
            )

    # Important: load only the requested MCP server.
    tools = await active_client.get_tools(
        server_name=server_name,
    )

    tool = next(
        (
            item
            for item in tools
            if item.name == tool_name
        ),
        None,
    )

    if tool is None:
        available_tools = (
            ", ".join(
                sorted(item.name for item in tools)
            )
            or "none"
        )

        raise RuntimeError(
            f"MCP tool '{tool_name}' was not found "
            f"on server '{server_name}'. "
            f"Available tools: {available_tools}"
        )

    return tool


def _extract_mcp_text(result) -> str:
    """
    Normalize an MCP tool result into plain, readable text.

    langchain_mcp_adapters often returns results as a list of content
    blocks - e.g. [{'type': 'text', 'text': '...', 'id': '...'}] -
    instead of a plain string. Every caller in this file expects a plain
    string (for display, truncation, and prompt-building), so this
    flattens that shape once, here, instead of leaking raw block
    structures into the rest of the app.
    """

    if isinstance(result, str):
        return result

    if isinstance(result, list):
        parts = []
        for item in result:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(str(item))
        return "\n".join(parts)

    return str(result)


async def _call_with_fallback(
    server_name: str,
    tool_name: str,
    tool_args: dict[str, Any],
):
    """
    Call an MCP tool, and if it fails and a *_FALLBACK key is configured
    for that server, automatically retry once using the fallback key.
    """

    try:
        tool = await _get_server_tool(server_name, tool_name)
        result = await tool.ainvoke(tool_args)
    except Exception as exc:
        if not _FALLBACK_KEY_SET.get(server_name):
            raise

        print(
            f"{server_name} call failed ({type(exc).__name__}: {exc}). "
            f"Retrying once with the {server_name} fallback key..."
        )
        tool = await _get_server_tool(server_name, tool_name, use_fallback=True)
        result = await tool.ainvoke(tool_args)

    return _extract_mcp_text(result)


# =========================================================
# MCP connection test
# =========================================================

async def get_all_tools() -> None:
    """
    Test every MCP server independently.

    One failed server will not stop the remaining tests.
    """

    for server_name in (
        "tavily",
        "aviationstack",
        "weather",
    ):
        try:
            tools = await client.get_tools(
                server_name=server_name,
            )

            tool_names = (
                ", ".join(
                    tool.name
                    for tool in tools
                )
                or "no tools"
            )

            print(
                f"{server_name}: OK -> {tool_names}"
            )

        except Exception as exc:
            print(
                f"{server_name}: FAILED -> "
                f"{type(exc).__name__}: {exc}"
            )


# =========================================================
# Tavily MCP
# =========================================================

async def tavily_mcp_search(query: str):
    return await _call_with_fallback(
        "tavily",
        "tavily_search",
        {"query": query},
    )


# =========================================================
# AviationStack MCP
# =========================================================

async def aviation_mcp_call(
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
):
    return await _call_with_fallback(
        "aviationstack",
        tool_name,
        tool_args or {},
    )


# =========================================================
# Weather MCP
# =========================================================

async def weather_mcp_search(city: str):
    return await _call_with_fallback(
        "weather",
        "get_current_weather",
        {"city": city},
    )


async def forecast_mcp_search(city: str):
    return await _call_with_fallback(
        "weather",
        "get_forecast",
        {"city": city},
    )


# =========================================================
# Destination extractor
# =========================================================

def extract_destination(query: str) -> str:
    prompt = f"""
Extract only the destination city or country from the travel request.

Travel request:
{query}

Return only the destination name.
Do not add any explanation.
"""

    response = invoke_llm(prompt)

    destination = str(
        response.content
    ).strip()

    if not destination:
        raise ValueError(
            "The destination could not be extracted."
        )

    return destination
