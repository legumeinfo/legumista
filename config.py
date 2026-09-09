"""Runtime configuration for the legumista MCP server.

This used to configure a whole research pipeline (crawl budgets, LLM endpoints, ideation
parameters, per-project YAML). legumista is now solely an MCP server, so what remains is
the three things the served tools actually need:

    WORKSPACE        the directory local file arguments are confined to
    contact_email()  the polite-pool mailto sent to OpenAlex/Crossref/Unpaywall
    tools_spec()     the tool-use doctrine appended to the server's MCP instructions

Everything is environment-driven; there is no config file to find, parse, or get wrong.
"""

import os

PKG_ROOT = os.path.dirname(os.path.abspath(__file__))


def _pkg_assets() -> str:
    """The bundled assets directory (prompts, images).

    Works from a source checkout and from an installed package, which are different
    layouts: the checkout has legumista_assets/ beside this file, an install has it as
    a package directory.
    """
    local = os.path.join(PKG_ROOT, "legumista_assets")
    if os.path.isdir(local):
        return local
    try:
        import legumista_assets
        return os.path.dirname(os.path.abspath(legumista_assets.__file__))
    except Exception:  # noqa: BLE001 - fall back to the source layout
        return local


PKG_ASSETS = _pkg_assets()

# Local file arguments (grep/read_file, and every path passed to samtools/bcftools) are
# confined to this directory. It is the launch directory unless overridden, which is what
# lets one installed server be pointed at different working trees.
WORKSPACE = os.path.abspath(os.environ.get("LEGUMISTA_HOME") or os.getcwd())


def contact_email() -> str:
    """Mailto for the scholarly APIs' polite pools.

    OpenAlex and Crossref give identified callers better rate limits and will contact you
    before blocking; anonymous traffic gets neither. The default is deliberately obviously
    fake so an unset value is visible in a request log rather than silently impersonating
    a real address.
    """
    return os.environ.get("LEGUMISTA_CONTACT_EMAIL", "you@example.org")


def tools_spec() -> str:
    """The tool-use doctrine served as the MCP server's `instructions`.

    Ships as package data; `LEGUMISTA_PROMPTS_DIR` overrides it for local editing without
    reinstalling. Empty string if absent — the server then falls back to a one-liner
    rather than failing to start.
    """
    for base in (os.environ.get("LEGUMISTA_PROMPTS_DIR"),
                 os.path.join(PKG_ASSETS, "prompts")):
        if not base:
            continue
        path = os.path.join(base, "tools_native.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return handle.read()
    return ""
