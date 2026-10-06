#!/usr/bin/env python3
"""`report_data_issue` — file a data defect in LIS's Data Store or mines as a GitHub issue.

Scope: the two data services, nothing else. A defect is what a curator would fix: a
README that does not parse or names the wrong collection, a taxid that disagrees with
its siblings, a typo'd identifier, a mine record that contradicts the store.

**The server verifies the observation; a curator judges it.** The agent states what it
saw (`observed`) in one field of one subject. Before anything is drafted, the server
re-reads that field from its source and refuses unless it says exactly that:

    datastore  readme.<key>   the collection's README at the catalog's commit in
                              datastore-metadata (raw.githubusercontent.com)
               readme         the README file itself: its name and whether it parses
               catalog.<key>  the resident catalog's record
    mine       <attribute>    a PathQuery for <mine>/<Class>/<primaryIdentifier>

The re-read is the issue's evidence. What the agent thinks the value should be
(`expected`, `reason`) is shown as unverified: a true observation can still be a
judgement call (a taxid that breaks its siblings' convention may be the right one).

**Nothing is filed without a person.** Where the client supports MCP elicitation, the
server asks the user directly and the agent cannot answer for them. Otherwise the first
call returns a preview and a token, and a second call with that token files it, marked
agent-confirmed; a public deployment refuses that fallback. The token is an HMAC over
the exact issue body, so nothing is stored between the two calls, and a body that
changed in between (the source moved) will not file.

**It authenticates as a GitHub App, never as a person.** Issues come from the App's bot
account; the App holds only the Issues permission and is installed only on the target
repository. For each repository the server signs a short-lived JWT with the App's
private key, exchanges it for an installation token narrowed to that one repository and
`issues: write`, and reuses it until shortly before its one-hour expiry. GitHub
publishes no official Python client, so this uses PyJWT for the signature and the same
redirect-refusing HTTP code as everything else here; neither the key nor a token ever
reaches a reply.

Configuration, never parameters: LEGUMISTA_GITHUB_APP_ID, LEGUMISTA_GITHUB_APP_KEY_FILE
(the App's private key, PEM), optionally LEGUMISTA_GITHUB_APP_INSTALLATION_ID,
LEGUMISTA_REPORT_REPOS (`service=owner/repo#label,...`), LEGUMISTA_METADATA_REPO and
LEGUMISTA_REPORT_DAILY_LIMIT. The tool is registered only when the server runs with
--allow-report and an App is configured, so it fails closed like the refresh webhook.
"""
import asyncio
import datetime
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config

from . import tools_lis, tools_mine
from .results import fail
from .tool import Tool
from .tools_catalog import catalog_stamp, controller
from .tools_native import _get, _validate_url

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
DEFAULT_REPOS = ("datastore=matthewwiese/datastore-metadata#datastore-issue,"
                 "mine=matthewwiese/datastore-metadata#mine-issue")
TOKEN_TTL = 600
_SECRET = os.urandom(32)              # per process: a restart invalidates open previews
_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*$")
_ALLOWED_LINK_HOSTS = ("legumeinfo.org", "lis.ncgr.org", "github.com", "doi.org",
                       "ncbi.nlm.nih.gov", "soybase.org", "peanutbase.org")
_DAILY = {"day": "", "count": 0}
_DAILY_LOCK = threading.Lock()


def _app_id():
    return os.environ.get("LEGUMISTA_GITHUB_APP_ID", "").strip()


def _key_file():
    return os.environ.get("LEGUMISTA_GITHUB_APP_KEY_FILE", "").strip()


def _app_configured() -> bool:
    """An App ID and a readable private-key file. Without both the tool is not served."""
    return bool(_app_id()) and bool(_key_file()) and os.path.isfile(_key_file())


def _repos():
    """{service: (owner/repo, label)} from LEGUMISTA_REPORT_REPOS."""
    out = {}
    for part in (os.environ.get("LEGUMISTA_REPORT_REPOS") or DEFAULT_REPOS).split(","):
        service, _, target = part.strip().partition("=")
        repo, _, label = target.partition("#")
        if service and re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
            out[service.strip()] = (repo.strip(), label.strip())
    return out


def _metadata_repo():
    return os.environ.get("LEGUMISTA_METADATA_REPO", "matthewwiese/datastore-metadata")


def _daily_limit():
    return int(os.environ.get("LEGUMISTA_REPORT_DAILY_LIMIT", "20"))


# --- making agent text inert ----------------------------------------------------------
def sanitize(text: str, limit: int) -> str:
    """Make agent- or source-supplied text safe to embed in an issue.

    Mentions and issue references are wrapped in code, so they notify and link nothing.
    HTML, comments included, is stripped, so the fingerprint comment cannot be forged.
    Links survive only to allowlisted hosts."""
    text = str(text or "")[:limit * 2]
    text = re.sub(r"(?s)<!--.*?-->", "", text)
    text = re.sub(r"<[^>]{0,500}>", "", text)

    def link(match):
        label, url = match.group(1), match.group(2)
        return f"[{label}]({url})" if _allowed(url) else f"{label} (link removed)"

    text = re.sub(r"\[([^\]]{0,200})\]\(([^)\s]{1,500})\)", link, text)
    text = re.sub(r"(?<![(\[])\bhttps?://[^\s)>\]]+",
                  lambda m: m.group(0) if _allowed(m.group(0)) else "(link removed)", text)
    text = re.sub(r"(?<![\w`/])@([A-Za-z0-9][A-Za-z0-9-]{0,38})", r"`@\1`", text)
    text = re.sub(r"(?<![\w`])((?:[\w.-]+/[\w.-]+)?#\d+)", r"`\1`", text)
    return text.strip()[:limit]


def _allowed(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _ALLOWED_LINK_HOSTS)


def _fence(text: str) -> str:
    """Put source text in a code block it cannot close."""
    return "```\n" + str(text).replace("```", "'''").strip() + "\n```"


def _norm(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    elif isinstance(value, dict):
        value = json.dumps(value, sort_keys=True)
    return re.sub(r"\s+", " ", str(value)).strip()


# --- verification ---------------------------------------------------------------------
class Refused(Exception):
    """The observation could not be confirmed; nothing is drafted."""


def _fetch_readme(record, commit):
    """(file name, text) of the collection's README at `commit`, or (None, None).
    Raises Refused when the source could not be read: a failure is not a finding."""
    repo = _metadata_repo()
    for name in (f"README.{record['id']}.yml", "README"):
        url = f"{RAW}/{repo}/{commit}/{record['path']}/{name}"
        try:
            return name, _get(url, accept="text/plain"), url
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise Refused(f"could not read {name} from {repo} at {commit[:8]} (HTTP "
                          f"{e.code}); retry later — nothing was filed") from None
        except Exception as e:  # noqa: BLE001
            raise Refused(f"could not read {name} from {repo} at {commit[:8]} "
                          f"({type(e).__name__}); retry later — nothing was filed") from None
    return None, None, f"{RAW}/{repo}/{commit}/{record['path']}/README.{record['id']}.yml"


def _readme_value(text, key):
    """The README's value for `key`: parsed YAML if it parses, else the first
    `key: value` line, so a malformed README still answers for its fields."""
    import yaml

    try:
        doc = yaml.safe_load(text)
        parses = isinstance(doc, dict)
    except yaml.YAMLError:
        doc, parses = None, False
    if parses and key in doc:
        return doc[key], parses
    match = re.search(rf"(?m)^{re.escape(key)}:[ \t]*(.*)$", text)
    return (match.group(1).strip() if match else None), parses


def _verify_datastore(subject, field, observed):
    record, err = tools_lis._lookup(subject)
    if record is None:
        raise Refused(err.removeprefix("error: "))
    ctl = controller()
    commit = (ctl.provenance().get("source_commit") or "") if ctl else ""
    stamp = catalog_stamp(ctl)
    ds_url = f"{record['base_url'].rstrip('/')}/"
    out = {"subject": record["path"], "record": record, "commit": commit,
           "links": [("Data Store", ds_url)]}
    if field.startswith("catalog."):
        key = field[len("catalog."):]
        if key not in record:
            raise Refused(f"the catalog record for {record['id']} has no field {key!r}")
        actual = record[key]
        if _norm(actual) != _norm(observed):
            raise Refused(f"the catalog says {key} = {_norm(actual)!r}, not "
                          f"{_norm(observed)!r} — nothing was filed")
        out.update(evidence=f"{key}: {_norm(actual)}",
                   source=f"the Legumista catalog, built from datastore-metadata "
                          f"{commit[:8]}")
        return out
    if not commit:
        raise Refused("the loaded catalog names no datastore-metadata commit, so its "
                      "README cannot be re-read")
    name, text, url = _fetch_readme(record, commit)
    out["links"].insert(0, (f"README at {commit[:8]}", url))
    source = f"datastore-metadata at {commit[:8]}"
    if field == "readme":
        if name is None:
            facts = [f"no README.{record['id']}.yml and no README in {record['path']}/"]
        else:
            import yaml
            facts = []
            if name != f"README.{record['id']}.yml":
                facts.append(f"the file is named '{name}', not 'README.{record['id']}.yml'")
            try:
                parsed = yaml.safe_load(text)
                if not isinstance(parsed, dict):
                    facts.append("it parses as YAML but not as a mapping of fields")
            except yaml.YAMLError as e:
                facts.append(f"it does not parse as YAML ({str(e).splitlines()[0]})")
            ident, _ = _readme_value(text, "identifier")
            if ident is not None and _norm(ident) != record["id"]:
                facts.append(f"its identifier is '{_norm(ident)}', not '{record['id']}'")
        if not facts:
            raise Refused(f"README.{record['id']}.yml exists, parses, and names its own "
                          "collection: there is no file-level defect to report")
        out.update(evidence="\n".join(facts) + (
            "\n\n" + text.strip()[:1500] if text else ""), source=source)
        return out
    if not field.startswith("readme."):
        raise Refused("for the datastore, 'field' is 'readme', 'readme.<key>' or "
                      "'catalog.<key>'")
    if name is None:
        raise Refused(f"{record['id']} has no README at {commit[:8]}; report "
                      "field='readme' instead")
    key = field[len("readme."):]
    actual, _parses = _readme_value(text, key)
    if _norm(actual) != _norm(observed):
        raise Refused(f"{name} at {commit[:8]} says {key} = {_norm(actual)!r}, not "
                      f"{_norm(observed)!r} — nothing was filed")
    line = re.search(rf"(?m)^{re.escape(key)}:.*$", text)
    out.update(evidence=line.group(0) if line else f"{key}: {_norm(actual)}", source=source)
    return out


def _verify_mine(subject, field, observed):
    parts = [p for p in str(subject).split("/") if p]
    if len(parts) != 3:
        raise Refused("for a mine, 'subject' is '<mine>/<Class>/<primaryIdentifier>', e.g. "
                      "'glycinemine/Gene/glyma.Wm82.gnm4.ann1.Glyma.12G040000'")
    mine, cls, ident = parts[0].lower(), parts[1], parts[2]
    if not re.fullmatch(r"[a-z0-9_-]+mine", mine) or not _FIELD_RE.match(cls):
        raise Refused(f"{parts[0]!r} / {cls!r} is not a mine name and class")
    known = tools_mine._known_mines()
    if known is not None and mine not in known and not tools_mine._mine_exists(mine):
        raise Refused(f"{mine} is not a published LIS mine")
    xml = tools_mine._pathquery([f"{cls}.{field}"],
                                [(f"{cls}.primaryIdentifier", "=", ident)])
    rows, _cols, err = tools_mine._run(mine, xml, 10)
    if err:
        raise Refused(f"{err.removeprefix('error: ')} — nothing was filed")
    if not rows:
        raise Refused(f"{mine} has no {cls} with primaryIdentifier {ident!r}")
    values = sorted({_norm(r[0]) for r in rows})
    if _norm(observed) not in values:
        raise Refused(f"{mine} says {cls}.{field} = {', '.join(repr(v) for v in values)} for "
                      f"{ident}, not {_norm(observed)!r} — nothing was filed")
    portal = (f"{tools_mine.MINES_BASE}/{mine}/portal.do?"
              + urllib.parse.urlencode({"externalids": ident, "class": cls}))
    return {"subject": f"{mine}/{cls}/{ident}", "record": None, "commit": "",
            "links": [(f"{mine} record", portal)],
            "evidence": f"{cls}.{field} = {', '.join(values)}\n(PathQuery: {cls} with "
                        f"primaryIdentifier = {ident})",
            "source": f"{mine}, queried"}


def _related(record):
    """Collections the catalog records as derived from `record`."""
    ctl = controller()
    if record is None or ctl is None:
        return []
    return sorted(c["path"] for c in ctl.collections
                  if record["id"] in (c.get("derived_from") or []))[:10]


# --- the issue ------------------------------------------------------------------------
def _fingerprint(service, subject, field):
    raw = f"{service}|{subject}|{field}".encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _draft(args, verified, confirmed_by):
    """(title, body, fingerprint) in the trackers' house style: the object and the
    defect in the title, then where, the exact evidence, the suggestion, the impact."""
    service, field = args["service"], args["field"]
    subject = verified["subject"]
    title = sanitize(args["summary"], 100).replace("`", "")
    ident = subject.rsplit("/", 1)[-1]
    if ident not in title:
        title = f"{title[:100 - len(ident) - 3]} ({ident})"
    links = " · ".join(f"[{label}]({url})" for label, url in verified["links"])
    lines = [f"`{subject}`, field `{field}` · {links}", "",
             f"Re-read from {verified['source']} before filing:", "",
             _fence(verified["evidence"]), ""]
    if args.get("expected"):
        lines += [f"Suggested by the reporting agent (unverified): "
                  f"{sanitize(args['expected'], 500)}", ""]
    if args.get("reason"):
        lines += [sanitize(args["reason"], 2000), ""]
    related = _related(verified["record"])
    if related:
        lines += ["May also affect collections derived from it: "
                  + ", ".join(f"`{r}`" for r in related), ""]
    fingerprint = _fingerprint(service, subject, field)
    when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    lines += ["---",
              f"Reported with Legumista ({confirmed_by}). The evidence above was re-read "
              f"from {verified['source']} on {when}; the suggestion and reasoning are the "
              "agent's and have not been checked.",
              f"<!-- legumista-fingerprint: {fingerprint} -->"]
    return title, "\n".join(lines), fingerprint


def _sign(body, exp):
    digest = hmac.new(_SECRET, f"{exp}|{body}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{digest[:32]}"


def _token_ok(token, body):
    exp, _, _sig = str(token or "").partition(".")
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(_sign(body, int(exp)), token)


# --- GitHub ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the Authorization header to wherever it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_GH_OPENER = urllib.request.build_opener(_NoRedirect)


class GitHubError(Exception):
    pass


# --- GitHub App authentication ----------------------------------------------------------
_TOKENS: dict = {}            # repo -> (installation token, expiry as epoch seconds)
_INSTALLATIONS: dict = {}     # repo -> installation id
_AUTH_LOCK = threading.Lock()
_TOKEN_MARGIN = 300           # mint a fresh token this many seconds before expiry


def _app_jwt() -> str:
    """A JWT identifying the App, signed RS256 with its private key. Valid nine minutes,
    issued a minute in the past to absorb clock skew (GitHub allows at most ten)."""
    import jwt

    try:
        with open(_key_file(), "rb") as fh:
            key = fh.read()
    except OSError as e:
        raise GitHubError(f"could not read the GitHub App's private key "
                          f"({type(e).__name__})") from None
    now = int(time.time())
    try:
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": _app_id()}, key,
                          algorithm="RS256")
    except Exception as e:  # noqa: BLE001 - never echo the key or its parse error text
        raise GitHubError(f"could not sign with the GitHub App's private key "
                          f"({type(e).__name__})") from None


def _installation_token(repo: str) -> str:
    """An installation token for `repo`, narrowed to that repository and issues: write."""
    with _AUTH_LOCK:
        cached = _TOKENS.get(repo)
        if cached and cached[1] - _TOKEN_MARGIN > time.time():
            return cached[0]
    app_jwt = _app_jwt()
    installation = (os.environ.get("LEGUMISTA_GITHUB_APP_INSTALLATION_ID", "").strip()
                    or _INSTALLATIONS.get(repo))
    if not installation:
        try:
            found = _request("GET", f"/repos/{repo}/installation", None, app_jwt)
        except GitHubError as e:
            if "HTTP 404" in str(e):
                raise GitHubError(f"the GitHub App is not installed on {repo}") from None
            raise
        installation = str(found["id"])
        _INSTALLATIONS[repo] = installation
    minted = _request("POST", f"/app/installations/{installation}/access_tokens",
                      {"repositories": [repo.split("/", 1)[1]],
                       "permissions": {"issues": "write"}}, app_jwt)
    expires = datetime.datetime.strptime(minted["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
    expiry = expires.replace(tzinfo=datetime.timezone.utc).timestamp()
    with _AUTH_LOCK:
        _TOKENS[repo] = (minted["token"], expiry)
    return minted["token"]


def reset_auth():
    """Forget cached installations and tokens. For tests."""
    with _AUTH_LOCK:
        _TOKENS.clear()
        _INSTALLATIONS.clear()


def _request(method, path, body, bearer):
    """One GitHub API call, never following a redirect. Errors carry GitHub's status and
    message, never the credential."""
    url = f"{API}{path}"
    _validate_url(url)
    req = urllib.request.Request(
        url, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {bearer}", "User-Agent": config.user_agent("legumista-report"),
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with _GH_OPENER.open(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read() or b"{}").get("message", "")
        except ValueError:
            message = ""
        raise GitHubError(f"GitHub returned HTTP {e.code}"
                          + (f": {message}" if message else "")) from None
    except urllib.error.URLError as e:
        raise GitHubError(f"could not reach GitHub ({e.reason})") from None


def _github(method, path, body=None):
    """A repository-scoped call (issues), authenticated as the App's installation."""
    match = re.match(r"^/repos/([\w.-]+/[\w.-]+)/", path)
    if not match:
        raise GitHubError(f"refusing a GitHub call outside a repository: {path}")
    return _request(method, path, body, _installation_token(match.group(1)))


def _existing(repo, label, fingerprint):
    """The URL of an issue (any state) already carrying this fingerprint, or ""."""
    marker = f"legumista-fingerprint: {fingerprint}"
    for page in range(1, 11):
        query = urllib.parse.urlencode({"labels": label, "state": "all", "per_page": 100,
                                        "page": page})
        issues = _github("GET", f"/repos/{repo}/issues?{query}") or []
        for issue in issues:
            if marker in (issue.get("body") or ""):
                return issue.get("html_url", "")
        if len(issues) < 100:
            return ""
    return ""


def _file(repo, label, title, body, fingerprint):
    existing = _existing(repo, label, fingerprint)
    if existing:
        return f"already reported, so nothing new was filed: {existing}"
    with _DAILY_LOCK:
        day = datetime.date.today().isoformat()
        if _DAILY["day"] != day:
            _DAILY.update(day=day, count=0)
        if _DAILY["count"] >= _daily_limit():
            return fail(f"this server has filed its {_daily_limit()} reports for today; "
                        "nothing was filed. Try again tomorrow.")
        _DAILY["count"] += 1
    issue = _github("POST", f"/repos/{repo}/issues",
                    {"title": title, "body": body, "labels": [label] if label else []})
    return f"filed: {issue.get('html_url', '(no URL returned)')}"


# --- confirmation ---------------------------------------------------------------------
async def _ask_user(title, body):
    """True/False from the user via MCP elicitation, or None when the client cannot ask."""
    try:
        from fastmcp.server.dependencies import get_context
        from mcp.types import ClientCapabilities, ElicitationCapability

        ctx = get_context()
        if not ctx.session.check_client_capability(
                ClientCapabilities(elicitation=ElicitationCapability())):
            return None
    except Exception:  # noqa: BLE001 - no request context or no capability: cannot ask
        return None
    try:
        result = await ctx.elicit(
            f"File this issue on GitHub?\n\nTitle: {title}\n\n{body}",
            response_type=["File this issue", "Do not file"])
    except Exception:  # noqa: BLE001 - the dialog failed: fall back to the preview
        return None
    return (type(result).__name__ == "AcceptedElicitation"
            and getattr(result, "data", None) == "File this issue")


async def _report(args):
    service = (args.get("service") or "").strip().lower()
    repos = _repos()
    if service not in ("datastore", "mine"):
        return fail("'service' must be 'datastore' or 'mine'.")
    if service not in repos:
        return fail(f"no repository is configured for {service!r} reports "
                    "(LEGUMISTA_REPORT_REPOS).")
    for key, limit in (("subject", 300), ("field", 100), ("observed", 500), ("summary", 100)):
        value = str(args.get(key) or "").strip()
        if not value:
            return fail(f"missing '{key}'.")
        if len(value) > limit:
            return fail(f"'{key}' is longer than {limit} characters.")
        args[key] = value
    if service == "mine" and not _FIELD_RE.match(args["field"]):
        return fail("for a mine, 'field' is an attribute path such as 'symbol' or "
                    "'organism.taxonId'.")
    if service == "datastore" and not re.fullmatch(
            r"readme|readme\.[\w-]+|catalog\.[\w-]+", args["field"]):
        return fail("for the datastore, 'field' is 'readme', 'readme.<key>' or "
                    "'catalog.<key>'.")
    if len(str(args.get("reason") or "")) > 2000:
        return fail("'reason' is longer than 2,000 characters.")

    verify = _verify_datastore if service == "datastore" else _verify_mine
    try:
        verified = await asyncio.to_thread(verify, args["subject"], args["field"],
                                           args["observed"])
    except Refused as e:
        return fail(f"not filed: {e}")
    repo, label = repos[service]
    public = config.deployment() == "public"
    confirm = str(args.get("confirm") or "").strip()

    if confirm:
        if public:
            return fail("this server files only with the user's own confirmation, which "
                        "this client cannot show; nothing was filed.")
        title, body, fingerprint = _draft(args, verified, "agent-confirmed after preview")
        if not _token_ok(confirm, body):
            return fail("the confirmation token is expired or does not match this report "
                        "(the source or the arguments changed). Preview it again.")
        return await _guarded_file(repo, label, title, body, fingerprint)

    title, body, fingerprint = _draft(args, verified, "confirmed by the user")
    answer = await _ask_user(title, body)
    if answer is True:
        return await _guarded_file(repo, label, title, body, fingerprint)
    if answer is False:
        return "the user declined; nothing was filed."
    if public:
        return fail("this server files reports only after the user confirms in a dialog, "
                    "and this client cannot show one; nothing was filed.")
    title, body, fingerprint = _draft(args, verified, "agent-confirmed after preview")
    token = _sign(body, int(time.time()) + TOKEN_TTL)
    return (f"PREVIEW — nothing has been filed. Show the user this issue for {repo} "
            f"(label {label}); if they agree, call report_data_issue again with the same "
            f"arguments and confirm='{token}' (valid {TOKEN_TTL // 60} minutes).\n\n"
            f"Title: {title}\n\n{body}")


async def _guarded_file(repo, label, title, body, fingerprint):
    try:
        out = await asyncio.to_thread(_file, repo, label, title, body, fingerprint)
    except GitHubError as e:
        return fail(f"{e}; nothing was filed.")
    return out


def report_tools(allow_report: bool = False) -> list:
    """The tool, or nothing: it exists only with --allow-report and a configured App."""
    if not (allow_report and _app_configured()):
        return []
    params = {
        "type": "object",
        "properties": {
            "service": {"type": "string", "enum": ["datastore", "mine"]},
            "subject": {"type": "string",
                        "description": "datastore: a collection path or id. mine: "
                                       "'<mine>/<Class>/<primaryIdentifier>'."},
            "field": {"type": "string",
                      "description": "datastore: 'readme', 'readme.<key>' or "
                                     "'catalog.<key>'. mine: an attribute such as "
                                     "'symbol'."},
            "observed": {"type": "string",
                         "description": "The value you saw; the server re-reads it and "
                                        "refuses if the source says otherwise. For "
                                        "field='readme', describe the defect."},
            "summary": {"type": "string",
                        "description": "The issue title: the object and the defect, at "
                                       "most 100 characters."},
            "expected": {"type": "string",
                         "description": "What it should be, if you know (shown as "
                                        "unverified)."},
            "reason": {"type": "string",
                       "description": "Why, at most 2,000 characters (shown as "
                                      "unverified)."},
            "confirm": {"type": "string",
                        "description": "The token from a preview, once the user agreed."},
        },
        "required": ["service", "subject", "field", "observed", "summary"],
    }
    return [Tool(
        name="report_data_issue",
        description=(
            "File a defect a curator would fix in LIS Data Store or mine data as a "
            "GitHub issue. The server re-reads the named field and refuses if it "
            "differs from what you observed; the user confirms before anything is "
            "filed. Not for missing files, CDS notes or site configuration."),
        parameters=params, read_only=False, run=_report)]
