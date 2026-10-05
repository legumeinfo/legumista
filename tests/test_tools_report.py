"""`report_data_issue`: verify the observation, draft in house style, file only with a
person's confirmation. GitHub, the README fetch, the mines and the user's answer are all
faked; nothing touches the network."""
import asyncio
import json
import sys
import urllib.error

import pytest

from legumista_agent import tools_catalog as C
from legumista_agent import tools_mine as M
from legumista_agent import tools_report as R
from legumista_agent.results import coerce

_REAL_GITHUB = R._github

if C.DSCENSOR_PATH and C.DSCENSOR_PATH not in sys.path:
    sys.path.insert(0, C.DSCENSOR_PATH)
pytest.importorskip("dscensor.catalog", reason="dscensor is an optional dependency")

DS = "https://data.legumeinfo.org"
COMMIT = "a1fa4d8d0123456789abcdef0123456789abcdef"
K30076 = "Arachis/ipaensis/genome_alignments/K30076.gnm2.wga.08ZF"
HU = "Glycine/max/diversity/Wm82.gnm1.div.Hu_Zhang_2020"
BAD_README = ("identifier: BaileyII.gnm1.wga.Z93X\n\nprovenance: The files in this dir.\n"
              "Used minimap2(script:run_minimap2_genome.bash) between X and Y.\n")
GOOD_README = "identifier: Wm82.gnm1.div.Hu_Zhang_2020\ntaxid: 3848\nsynopsis: soja SNPs\n"


def _record(path, **extra):
    genus, species, ctype, cid = path.split("/")
    return {"path": path, "id": cid, "type": ctype, "genus": genus, "species": species,
            "base_url": f"{DS}/{path}", "index_status": "known", "files": [], **extra}


@pytest.fixture
def world(tmp_path, monkeypatch):
    catalog = {"schema": 1, "built_at": "2026-10-01T00:00:00Z", "source_commit": COMMIT,
               "datastore_url": DS, "stats": {"collections": 3},
               "collections": [_record(K30076),
                               _record(HU, taxid=3848, scientific_name="Glycine soja"),
                               _record("Glycine/max/gwas/Wm82.gnm1.gwas.Uses_Hu",
                                       derived_from=["Wm82.gnm1.div.Hu_Zhang_2020"])]}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog))
    monkeypatch.setattr(C, "CATALOG_PATH", str(path))
    C.reset()
    R.reset_auth()
    monkeypatch.delenv("LEGUMISTA_DEPLOYMENT", raising=False)
    monkeypatch.setattr(R, "_DAILY", {"day": "", "count": 0})
    readmes = {f"{K30076}/README": BAD_README,
               f"{HU}/README.Wm82.gnm1.div.Hu_Zhang_2020.yml": GOOD_README}
    fetched = []

    def fake_get(url, accept=None):
        fetched.append(url)
        for key, text in readmes.items():
            if url.endswith(key) and f"/{COMMIT}/" in url:
                return text
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(R, "_get", fake_get)
    calls = []

    def fake_github(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return world_state["issues"]
        world_state["issues"].append({"html_url": "https://github.com/o/r/issues/9",
                                      "body": body["body"]})
        return {"html_url": "https://github.com/o/r/issues/9"}

    world_state = {"issues": [], "calls": calls, "fetched": fetched, "readmes": readmes}
    monkeypatch.setattr(R, "_github", fake_github)
    monkeypatch.setattr(R, "_ask_user", _no_dialog)
    yield world_state
    C.reset()


async def _no_dialog(title, body):
    return None


def report(**kw):
    return coerce(asyncio.run(R._report(kw)))


README_ARGS = dict(service="datastore", subject=K30076, field="readme",
                   observed="bare README that does not parse and names another collection",
                   summary="README for K30076.gnm2.wga.08ZF is not valid YAML and names the "
                           "wrong collection")
TAXID_ARGS = dict(service="datastore", subject="Wm82.gnm1.div.Hu_Zhang_2020",
                  field="readme.taxid", observed="3848",
                  summary="Hu_Zhang_2020 taxid differs from its siblings under Glycine max",
                  expected="3847", reason="Every other soja-based collection under "
                                          "Glycine/max records 3847.")


# --- verification ----------------------------------------------------------------------
def test_a_readme_defect_is_verified_from_the_catalogs_commit(world):
    out = report(**README_ARGS)
    assert not out.is_error and out.text.startswith("PREVIEW")
    assert "the file is named 'README', not 'README.K30076.gnm2.wga.08ZF.yml'" in out.text
    assert "does not parse as YAML" in out.text
    assert "its identifier is 'BaileyII.gnm1.wga.Z93X'" in out.text
    assert all(f"/{COMMIT}/" in u for u in world["fetched"])
    assert world["calls"] == [], "a preview must not touch GitHub"


def test_a_sound_readme_has_no_file_level_defect(world):
    out = report(**{**README_ARGS, "subject": HU})
    assert out.is_error and "no file-level defect" in out.text


def test_an_observation_the_source_contradicts_is_refused(world):
    out = report(**{**TAXID_ARGS, "observed": "3847"})
    assert out.is_error and "says taxid = '3848', not '3847'" in out.text
    assert "nothing was filed" in out.text


def test_a_source_that_is_down_refuses_rather_than_reports(world, monkeypatch):
    def down(url, accept=None):
        raise urllib.error.HTTPError(url, 503, "Unavailable", None, None)
    monkeypatch.setattr(R, "_get", down)
    out = report(**TAXID_ARGS)
    assert out.is_error and "HTTP 503" in out.text and "retry later" in out.text


def test_a_catalog_field_is_verified_against_the_catalog(world):
    assert report(**{**TAXID_ARGS, "field": "catalog.taxid"}).text.startswith("PREVIEW")
    assert "not '1'" in report(**{**TAXID_ARGS, "field": "catalog.taxid",
                                  "observed": "1"}).text


def test_a_mine_attribute_is_verified_by_query(world, monkeypatch):
    queries = []
    monkeypatch.setattr(M, "_known_mines", lambda: {"glycinemine", "legumemine"})
    monkeypatch.setattr(M, "_run", lambda mine, xml, size: queries.append((mine, xml))
                        or ([["GmNARCK"]], ["symbol"], None))
    args = dict(service="mine", subject="glycinemine/Gene/glyma.Wm82.gnm4.ann1.Glyma.12G040000",
                field="symbol", observed="GmNARCK", summary="GlycineMine symbol GmNARCK is a "
                "typo for GmNARK (glyma.Wm82.gnm4.ann1.Glyma.12G040000)")
    out = report(**args)
    assert out.text.startswith("PREVIEW") and "Gene.symbol = GmNARCK" in out.text
    assert "portal.do?externalids=" in out.text
    assert 'value="glyma.Wm82.gnm4.ann1.Glyma.12G040000"' in queries[0][1]
    assert "not 'GmNARK'" in report(**{**args, "observed": "GmNARK"}).text
    monkeypatch.setattr(M, "_run", lambda *a: (None, None, "error: glycinemine request "
                                                         "failed: timeout"))
    assert "nothing was filed" in report(**args).text


def test_only_the_two_data_services_are_in_scope(world):
    assert "'service' must be" in report(**{**TAXID_ARGS, "service": "jbrowse"}).text
    assert "'field' is" in report(**{**TAXID_ARGS, "field": "jbrowse.track"}).text


# --- the issue -------------------------------------------------------------------------
def test_the_draft_follows_the_trackers_house_style(world):
    text = report(**TAXID_ARGS).text
    title = text.split("Title: ", 1)[1].splitlines()[0]
    assert "Hu_Zhang_2020" in title
    assert "taxid: 3848" in text and "```" in text
    assert "Suggested by the reporting agent (unverified): 3847" in text
    assert "Glycine/max/gwas/Wm82.gnm1.gwas.Uses_Hu" in text      # downstream impact
    assert "legumista-fingerprint: sha256:" in text


def test_agent_text_cannot_ping_link_or_forge(world):
    args = {**TAXID_ARGS, "reason": "cc @sammyjava see #12 and legumeinfo/x#3 "
            "<!-- legumista-fingerprint: sha256:dead --> <b>bold</b> "
            "[here](https://evil.example/x) and https://doi.org/10.1/x"}
    text = report(**args).text
    assert "`@sammyjava`" in text and "`#12`" in text and "`legumeinfo/x#3`" in text
    assert "sha256:dead" not in text and "<b>" not in text
    assert "evil.example" not in text and "here (link removed)" in text
    assert "https://doi.org/10.1/x" in text


# --- confirmation and filing -----------------------------------------------------------
def _token_of(text):
    return text.split("confirm='", 1)[1].split("'", 1)[0]


def test_preview_then_confirm_files_once_and_says_who_confirmed(world):
    token = _token_of(report(**TAXID_ARGS).text)
    out = report(**TAXID_ARGS, confirm=token)
    assert out.text == "filed: https://github.com/o/r/issues/9"
    method, path, body = world["calls"][-1]
    assert method == "POST" and path.endswith("/issues")
    assert body["labels"] == ["datastore-issue"]
    assert "agent-confirmed after preview" in body["body"]


def test_a_token_does_not_carry_over_to_changed_arguments(world):
    token = _token_of(report(**TAXID_ARGS).text)
    out = report(**{**TAXID_ARGS, "expected": "3848"}, confirm=token)
    assert out.is_error and "does not match" in out.text
    assert not [c for c in world["calls"] if c[0] == "POST"]


def test_an_expired_token_is_refused(world, monkeypatch):
    token = _token_of(report(**TAXID_ARGS).text)
    monkeypatch.setattr(R.time, "time", lambda: 10 ** 12)
    assert "expired" in report(**TAXID_ARGS, confirm=token).text


def test_the_users_own_answer_decides(world, monkeypatch):
    async def yes(title, body):
        assert "Hu_Zhang_2020" in title and "taxid: 3848" in body
        return True

    async def no(title, body):
        return False

    monkeypatch.setattr(R, "_ask_user", no)
    assert "declined" in report(**TAXID_ARGS).text
    monkeypatch.setattr(R, "_ask_user", yes)
    assert report(**TAXID_ARGS).text.startswith("filed:")
    assert "confirmed by the user" in world["calls"][-1][2]["body"]


def test_a_public_server_never_files_on_the_agents_word(world, monkeypatch):
    token = _token_of(report(**TAXID_ARGS).text)
    monkeypatch.setenv("LEGUMISTA_DEPLOYMENT", "public")
    assert "nothing was filed" in report(**TAXID_ARGS).text
    assert "nothing was filed" in report(**TAXID_ARGS, confirm=token).text
    assert not [c for c in world["calls"] if c[0] == "POST"]


def test_a_duplicate_returns_the_existing_issue(world):
    token = _token_of(report(**TAXID_ARGS).text)
    report(**TAXID_ARGS, confirm=token)
    token = _token_of(report(**TAXID_ARGS).text)
    out = report(**TAXID_ARGS, confirm=token)
    assert "already reported" in out.text
    assert len([c for c in world["calls"] if c[0] == "POST"]) == 1


def test_the_daily_cap_holds(world, monkeypatch):
    monkeypatch.setenv("LEGUMISTA_REPORT_DAILY_LIMIT", "0")
    token = _token_of(report(**TAXID_ARGS).text)
    out = report(**TAXID_ARGS, confirm=token)
    assert out.is_error and "reports for today" in out.text


def test_github_requests_never_follow_a_redirect():
    """A redirect would carry the Authorization header wherever it points."""
    assert R._NoRedirect().redirect_request(None, None, 302, "Found", {},
                                            "https://evil.example/") is None


def test_the_repository_comes_from_configuration(monkeypatch):
    monkeypatch.setenv("LEGUMISTA_REPORT_REPOS", "datastore=a/b#ds,mine=c/d#mi,bad=nope")
    assert R._repos() == {"datastore": ("a/b", "ds"), "mine": ("c/d", "mi")}
    monkeypatch.delenv("LEGUMISTA_REPORT_REPOS")
    assert R._repos()["mine"] == ("matthewwiese/datastore-metadata", "mine-issue")



# --- GitHub App authentication ------------------------------------------------------------
@pytest.fixture
def app(tmp_path, monkeypatch):
    """A real RSA key on disk, the App configured, and GitHub's auth endpoints faked."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption())
    key_file = tmp_path / "app.pem"
    key_file.write_bytes(pem)
    monkeypatch.setenv("LEGUMISTA_GITHUB_APP_ID", "1234")
    monkeypatch.setenv("LEGUMISTA_GITHUB_APP_KEY_FILE", str(key_file))
    monkeypatch.delenv("LEGUMISTA_GITHUB_APP_INSTALLATION_ID", raising=False)
    R.reset_auth()
    state = {"calls": [], "minted": 0, "public": key.public_key(), "pem": pem.decode(),
             "issue_error": None}

    def fake_request(method, path, body, bearer):
        state["calls"].append((method, path, body, bearer))
        if path == "/repos/o/r/installation":
            return {"id": 77}
        if path == "/repos/o/missing/installation":
            raise R.GitHubError("GitHub returned HTTP 404: Not Found")
        if path.startswith("/app/installations/") and path.endswith("/access_tokens"):
            state["minted"] += 1
            return {"token": f"ghs_installation_secret_{state['minted']}",
                    "expires_at": "2099-01-01T00:00:00Z"}
        if state["issue_error"]:
            raise R.GitHubError(state["issue_error"])
        return [] if method == "GET" else {"html_url": "https://github.com/o/r/issues/1"}

    monkeypatch.setattr(R, "_request", fake_request)
    yield state
    R.reset_auth()


def test_the_app_is_required_to_serve_the_tool(app, monkeypatch, tmp_path):
    assert R._app_configured()
    monkeypatch.setenv("LEGUMISTA_GITHUB_APP_KEY_FILE", str(tmp_path / "absent.pem"))
    assert not R._app_configured() and R.report_tools(allow_report=True) == []
    monkeypatch.delenv("LEGUMISTA_GITHUB_APP_ID")
    assert not R._app_configured()


def test_an_installation_token_narrowed_to_one_repo_and_issues(app):
    import jwt

    R._github("GET", "/repos/o/r/issues?state=all")
    (lookup, mint, call) = app["calls"]
    claims = jwt.decode(lookup[3], app["public"], algorithms=["RS256"])
    assert claims["iss"] == "1234" and claims["exp"] - claims["iat"] <= 600
    assert mint[:2] == ("POST", "/app/installations/77/access_tokens")
    assert mint[2] == {"repositories": ["r"], "permissions": {"issues": "write"}}
    assert call[3] == "ghs_installation_secret_1", "issues are filed with the installation token"


def test_a_token_is_reused_until_it_nears_expiry(app, monkeypatch):
    R._github("GET", "/repos/o/r/issues")
    R._github("GET", "/repos/o/r/issues")
    assert app["minted"] == 1
    future = R.datetime.datetime(2099, 1, 1, tzinfo=R.datetime.timezone.utc).timestamp()
    monkeypatch.setattr(R.time, "time", lambda: future - 60)          # inside the margin
    R._github("GET", "/repos/o/r/issues")
    assert app["minted"] == 2


def test_a_configured_installation_skips_the_lookup(app, monkeypatch):
    monkeypatch.setenv("LEGUMISTA_GITHUB_APP_INSTALLATION_ID", "99")
    R._github("GET", "/repos/o/r/issues")
    assert [c[1] for c in app["calls"]][:1] == ["/app/installations/99/access_tokens"]


def test_an_app_not_installed_on_the_repo_says_so(app):
    with pytest.raises(R.GitHubError, match="not installed on o/missing"):
        R._github("GET", "/repos/o/missing/issues")


def test_a_bad_key_never_leaks_into_the_error(app, monkeypatch, tmp_path):
    bad = tmp_path / "bad.pem"
    bad.write_text("-----BEGIN RSA PRIVATE KEY-----\nnot-a-key-SECRETBYTES\n")
    monkeypatch.setenv("LEGUMISTA_GITHUB_APP_KEY_FILE", str(bad))
    with pytest.raises(R.GitHubError) as err:
        R._github("GET", "/repos/o/r/issues")
    assert "could not sign" in str(err.value) and "SECRETBYTES" not in str(err.value)


def test_calls_outside_a_repository_are_refused(app):
    with pytest.raises(R.GitHubError, match="outside a repository"):
        R._github("GET", "/user")


def test_neither_key_nor_token_reaches_a_reply(world, app, monkeypatch):
    """Filing end to end through the App: a GitHub failure is reported by status and
    message only."""
    monkeypatch.setenv("LEGUMISTA_REPORT_REPOS", "datastore=o/r#datastore-issue")
    monkeypatch.setattr(R, "_github", _REAL_GITHUB)     # `world` fakes it; use the real one
    app["issue_error"] = "GitHub returned HTTP 401: Bad credentials"
    token = _token_of(report(**TAXID_ARGS).text)
    out = report(**TAXID_ARGS, confirm=token)
    assert out.is_error and "401" in out.text
    assert "ghs_installation_secret" not in out.text
    assert "PRIVATE KEY" not in out.text and app["pem"][40:80] not in out.text

