"""Run browser regressions with: uv run --with playwright pytest tests/test_agent_links_browser.py."""

import json
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from urllib.error import URLError
from urllib.request import urlopen

from datasette.app import Datasette
import pytest
from ulid import ULID

from datasette_agent.schema import SCHEMA_SQL

playwright = pytest.importorskip("playwright.sync_api")

EXTERNAL_URL = "https://example.com/docs?secret=private#section"
LONG_URL = "https://example.com/?query=" + "encoded%20value%20" * 500


@pytest.fixture(scope="module", params=["", "/prefix"])
def server(request, tmp_path_factory):
    directory = tmp_path_factory.mktemp("link-server")
    internal = directory / "internal.db"
    config = directory / "datasette.json"
    config.write_text(json.dumps({"permissions": {"datasette-agent": True}}))
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    prefix = request.param
    base = origin + prefix
    conversation_id = str(ULID())
    message = {
        "role": "assistant",
        "parts": [
            {
                "type": "reasoning",
                "text": "Thinking [Reasoning external](https://example.com/reasoning).",
            },
            {
                "type": "text",
                "text": (
                    f"[External]({EXTERNAL_URL})\n\n"
                    f"[Internal]({prefix}/-/versions)\n\n"
                    f"[Absolute internal]({base}/-/versions)\n\n"
                    "[Fragment](#section)\n\n"
                    "https://example.com/raw?secret=private\n\n"
                    "[Unsafe](javascript:window.agentLinkExecuted=true)\n\n"
                    f"[Long external]({LONG_URL})"
                ),
            },
        ],
    }

    with sqlite3.connect(internal) as db:
        db.executescript(SCHEMA_SQL)
        created = "2020-01-01T00:00:00+00:00"
        db.execute(
            "INSERT INTO agent_conversations "
            "(id, actor_id, title, model_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [conversation_id, "root", "Old conversation", "echo", created, created],
        )
        db.execute(
            "INSERT INTO agent_messages "
            "(conversation_id, role, message_json, created_at) VALUES (?, ?, ?, ?)",
            [conversation_id, "assistant", json.dumps(message), created],
        )
    ds = Datasette(memory=True, secret="link-tests")
    cookie = ds.client.actor_cookie({"id": "root"})
    ds.close()
    with (directory / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "datasette",
                "--internal",
                str(internal),
                "--config",
                str(config),
                "--secret",
                "link-tests",
                "-p",
                str(port),
                "-s",
                "base_url",
                (prefix or "") + "/",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(100):
                try:
                    with urlopen(base + "/-/versions.json", timeout=1):
                        break
                except (URLError, TimeoutError):
                    if process.poll() is not None:
                        log.seek(0)
                        pytest.fail(log.read())
                    time.sleep(0.1)
            else:
                pytest.fail("Test Datasette server did not start")
            yield {
                "origin": origin,
                "base": base,
                "prefix": prefix,
                "url": f"{base}/-/agent/{conversation_id}",
                "cookie": cookie,
            }
        finally:
            process.terminate()
            process.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(browser, server):
    context = browser.new_context()
    context.add_cookies(
        [{"name": "ds_actor", "value": server["cookie"], "url": server["origin"]}]
    )
    context.add_init_script("""
      window.externalHrefAssignments = [];
      const setAttribute = Element.prototype.setAttribute;
      Element.prototype.setAttribute = function(name, value) {
        if (this.tagName === "A" && name.toLowerCase() === "href" &&
            new URL(value, document.baseURI).origin !== location.origin) {
          window.externalHrefAssignments.push(value);
        }
        return setAttribute.call(this, name, value);
      };
    """)
    page = context.new_page()
    page.set_default_timeout(5000)
    external_requests = []
    context.on(
        "request",
        lambda request: (
            external_requests.append(request)
            if request.url.startswith("https://example.com/")
            else None
        ),
    )
    context.route(
        "https://example.com/**",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body="<h1>External destination</h1>"
        ),
    )
    response = page.goto(server["url"])
    assert response.status == 200
    page.wait_for_function('typeof window.sendMessage === "function"')
    page.external_requests = external_requests
    yield page
    context.close()


def test_saved_external_link_requires_confirmation(page, server):
    link = page.get_by_role("link", name="External", exact=True)
    assert link.get_attribute("href") is None
    assert page.evaluate("window.externalHrefAssignments") == []
    link.click()
    dialog = page.get_by_role("dialog", name="Open external link")
    assert dialog.locator(".agent-link-url").inner_text() == EXTERNAL_URL
    assert page.external_requests == []
    assert page.url == server["url"]
    assert dialog.get_by_role("button", name="Cancel").evaluate(
        "el => el === document.activeElement"
    )
    dialog.get_by_role("button", name="Cancel").click()
    assert not dialog.is_visible()
    assert page.external_requests == []
    link.click()
    with page.expect_popup() as popup_info:
        dialog.get_by_role("button", name="Open link").click()
    popup = popup_info.value
    popup.wait_for_load_state()
    assert popup.url == EXTERNAL_URL
    assert popup.evaluate("window.opener") is None
    assert len(page.external_requests) == 1
    assert "referer" not in page.external_requests[0].headers
    assert page.url == server["url"]


@pytest.mark.parametrize(
    "gesture", ["keyboard", "middle", "Control", "Meta", "Shift", "Alt"]
)
def test_external_link_gestures_cannot_bypass_confirmation(page, gesture):
    link = page.get_by_role("link", name="External", exact=True)
    if gesture == "keyboard":
        link.focus()
        page.keyboard.press("Enter")
    elif gesture == "middle":
        link.click(button="middle")
    else:
        link.click(modifiers=[gesture])
    dialog = page.get_by_role("dialog", name="Open external link")
    playwright.expect(dialog).to_be_visible()
    assert page.external_requests == []
    assert len(page.context.pages) == 1
    page.keyboard.press("Escape")
    playwright.expect(dialog).not_to_be_visible()
    playwright.expect(link).to_be_focused()


def test_internal_links_keep_panel_and_native_navigation(page, server):
    internal = page.get_by_role("link", name="Internal", exact=True)
    absolute = page.get_by_role("link", name="Absolute internal", exact=True)
    fragment = page.get_by_role("link", name="Fragment", exact=True)
    assert internal.get_attribute("href") == server["prefix"] + "/-/versions"
    assert absolute.get_attribute("href") == server["base"] + "/-/versions"
    assert fragment.get_attribute("href") == "#section"
    fragment.click()
    assert page.url == server["url"] + "#section"
    internal.click()
    assert (
        page.evaluate("window.datasetteAgentPanel.currentUrl()")
        == server["prefix"] + "/-/versions"
    )
    assert not page.locator(".agent-link-dialog").count()
    assert page.external_requests == []


def test_raw_urls_and_saved_reasoning_are_protected(page):
    raw = page.get_by_role(
        "link", name="https://example.com/raw?secret=private", exact=True
    )
    assert raw.get_attribute("href") is None
    raw.click()
    page.keyboard.press("Escape")
    page.locator(".agent-reasoning summary").click()
    page.get_by_role("link", name="Reasoning external", exact=True).click()
    assert (
        page.locator(".agent-link-url").inner_text() == "https://example.com/reasoning"
    )
    assert page.external_requests == []


def test_streamed_links_are_protected_before_receiving_a_href(page):
    events = []
    for event, text in [
        ("reasoning_chunk", "[Streamed reasoning](https://example.com/thinking)"),
        (
            "text_chunk",
            "[Streamed external](https://example.com/streamed?secret=private)",
        ),
    ]:
        for char in text:
            events.append(f"event: {event}\ndata: {json.dumps({'content': char})}\n\n")
    events.append("event: done\ndata: {}\n\n")
    page.route(
        "**/stream",
        lambda route: route.fulfill(
            status=200, content_type="text/event-stream", body="".join(events)
        ),
    )
    page.evaluate('() => window.sendMessage("", "Test links")')
    assert page.evaluate("window.externalHrefAssignments") == []
    link = page.get_by_role("link", name="Streamed external", exact=True)
    assert link.get_attribute("href") is None
    link.click()
    assert (
        page.locator(".agent-link-url").inner_text()
        == "https://example.com/streamed?secret=private"
    )
    page.keyboard.press("Escape")
    page.locator(".agent-reasoning summary").last.click()
    page.get_by_role("link", name="Streamed reasoning", exact=True).click()
    assert page.external_requests == []


def test_long_url_fits_dialog_and_keeps_actions_visible(page, tmp_path):
    page.set_viewport_size({"width": 800, "height": 500})
    page.get_by_role("link", name="Long external", exact=True).click()
    dialog = page.get_by_role("dialog", name="Open external link")
    assert dialog.locator(".agent-link-url").inner_text() == LONG_URL
    for locator in [dialog, dialog.get_by_role("button", name="Open link")]:
        box = locator.bounding_box()
        assert 0 <= box["x"] and box["x"] + box["width"] <= 800
        assert 0 <= box["y"] and box["y"] + box["height"] <= 500
    assert dialog.locator(".agent-link-url").evaluate(
        "el => el.scrollHeight > el.clientHeight"
    )
    page.screenshot(path=str(tmp_path / "link-dialog.png"))
    assert page.external_requests == []


def test_executable_link_scheme_cannot_run(page):
    link = page.locator(".agent-message-assistant a").filter(has_text="Unsafe")
    assert link.get_attribute("href") is None
    assert link.get_attribute("data-agent-external-url") is None
    link.click()
    assert page.evaluate("window.agentLinkExecuted") is None
    assert page.external_requests == []


@pytest.mark.parametrize(
    "path",
    ["/-/agent?x=1", "/-/agent#top", "/-/versions/../agent", "/-/%61gent"],
)
def test_panel_refuses_agent_pages(page, server, path):
    url = server["prefix"] + path
    page.evaluate("url => window.datasetteAgentPanel.open(url, {source: 'agent'})", url)
    assert page.evaluate("window.datasetteAgentPanel.currentUrl()") is None
    assert page.locator(".agent-panel-frame").first.get_attribute("src") is None
    # A normal page still opens
    page.evaluate(
        "url => window.datasetteAgentPanel.open(url, {source: 'agent'})",
        server["prefix"] + "/-/versions",
    )
    assert (
        page.evaluate("window.datasetteAgentPanel.currentUrl()")
        == server["prefix"] + "/-/versions"
    )
