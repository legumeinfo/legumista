"""Runtime configuration for the legumista MCP server.

This used to configure a whole research pipeline (crawl budgets, LLM endpoints, ideation
parameters, per-project YAML). legumista is now solely an MCP server, so what remains is
the things the served tools actually need:

    WORKSPACE        the directory local file arguments are confined to
    contact_email()  the polite-pool mailto sent to OpenAlex/Crossref/Unpaywall
    user_agent()     the User-Agent every outbound request sends, with the package version
    deployment()     'local' or 'public': how the tools are advertised to clients
    tools_spec()     the tool-use doctrine appended to the server's MCP instructions

Everything is environment-driven; there is no config file to find, parse, or get wrong.
"""

import functools
import os
from importlib import metadata

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
    # `or` rather than a get() default: an env var set to the empty string (easy to do
    # from a compose `environment:` block or an `EMAIL=` line) would otherwise be sent to
    # OpenAlex as the mailto, which is worse than the obviously-fake default.
    return os.environ.get("LEGUMISTA_CONTACT_EMAIL") or "you@example.org"


@functools.lru_cache(maxsize=None)
def version() -> str:
    """The package version pyproject.toml sets, so a release bump reaches every request
    without another edit.

    In a source checkout pyproject.toml sits beside this file and is read directly: an
    editable install's metadata keeps the version from its last `pip install`, so after
    a bump it would be stale. Installed from a wheel or in the image, the file is absent
    and the package metadata answers. "unknown" if neither does."""
    pyproject = os.path.join(PKG_ROOT, "pyproject.toml")
    if os.path.isfile(pyproject):
        import tomllib
        with open(pyproject, "rb") as handle:
            found = (tomllib.load(handle).get("project") or {}).get("version")
        if found:
            return str(found)
    try:
        return metadata.version("legumista")
    except metadata.PackageNotFoundError:
        return "unknown"


def user_agent(product: str = "legumista", mailto: bool = False) -> str:
    """The User-Agent for an outbound request. `mailto` adds the polite-pool contact,
    which the scholarly APIs read from the User-Agent."""
    contact = f"; mailto:{contact_email()}" if mailto else ""
    return f"{product}/{version()} (+https://github.com/legumeinfo/legumista{contact})"


def deployment() -> str:
    """'public' or 'local' (the default), from LEGUMISTA_DEPLOYMENT.

    It decides one thing: how write-capable tools are advertised. Clients use the MCP
    `readOnlyHint` to decide whether to ask the user before each call. Locally they
    should ask before a tool writes a file or files an issue, so the hints stay honest.
    A public host advertises every tool as read-only so its users are not prompted; its
    own checks stand in for the prompt (a confirmation dialog the user answers before
    any issue is filed, and the workspace sandbox and per-file caps for files).
    Anything other than 'public' is 'local': an unrecognised value fails safe, toward
    prompting."""
    value = (os.environ.get("LEGUMISTA_DEPLOYMENT") or "").strip().lower()
    return "public" if value == "public" else "local"


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
