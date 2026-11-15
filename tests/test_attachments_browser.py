"""Run browser tests with: uv run pytest tests/test_attachments_browser.py."""

import base64
import json
import socket
import subprocess
import sys
import time
from contextlib import closing
from urllib.error import URLError
from urllib.request import urlopen

from datasette.app import Datasette
import pytest

playwright = pytest.importorskip("playwright.sync_api")
pytest.importorskip("datasette_files")

PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)
PNG = base64.b64decode(PNG_BASE64)
PNG_FILE = {"name": "shot.png", "mimeType": "image/png", "buffer": PNG}

# Fire a paste or drop of one PNG at an element, as the browser would.
DISPATCH_FILE_EVENT = """
([selector, eventType, name, base64]) => {
  const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
  const transfer = new DataTransfer();
  transfer.items.add(new File([bytes], name, {type: "image/png"}));
  const target = document.querySelector(selector);
  if (eventType === "paste") {
    target.dispatchEvent(new ClipboardEvent("paste", {
      clipboardData: transfer, bubbles: true, cancelable: true,
    }));
  } else {
    for (const type of ["dragenter", "dragover", "drop"]) {
      target.dispatchEvent(new DragEvent(type, {
        dataTransfer: transfer, bubbles: true, cancelable: true,
      }));
    }
  }
}
"""


@pytest.fixture(scope="module", params=["", "/prefix"])
def server(request, tmp_path_factory):
    directory = tmp_path_factory.mktemp("attachments-server")
    uploads = directory / "uploads"
    uploads.mkdir()
    config = directory / "datasette.json"
    config.write_text(
        json.dumps(
            {
                "permissions": {"datasette-agent": True},
                "plugins": {
                    "datasette-llm": {"default_model": "echo", "models": ["echo"]},
                    "datasette-agent": {"attachments_source": "agent-attachments"},
                    "datasette-files": {
                        "sources": {
                            "agent-attachments": {
                                "storage": "filesystem",
                                "config": {"root": str(uploads)},
                                "audience": "collaborators",
                            }
                        }
                    },
                },
            }
        )
    )
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    prefix = request.param
    base = origin + prefix
    ds = Datasette(memory=True, secret="attachment-tests")
    cookie = ds.client.actor_cookie({"id": "root"})
    ds.close()
    with (directory / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "datasette",
                "--internal",
                str(directory / "internal.db"),
                "--config",
                str(config),
                "--secret",
                "attachment-tests",
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
            yield {"origin": origin, "base": base, "prefix": prefix, "cookie": cookie}
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
    page = context.new_page()
    page.set_default_timeout(5000)
    page.errors = []
    page.on("pageerror", lambda error: page.errors.append(str(error)))
    yield page
    assert page.errors == []
    context.close()


def done_chips(page):
    return page.locator('.agent-attachment-chip[data-status="done"]')


def start_chat(page, server, message="Start"):
    "A new conversation with one plain message already answered."
    assert page.goto(server["base"] + "/-/agent").status == 200
    page.fill("#new-message", message)
    page.get_by_role("button", name="Start chat").click()
    page.wait_for_url("**/-/agent/*")
    playwright.expect(page.locator(".agent-message-assistant")).to_have_count(1)
    playwright.expect(page.get_by_role("button", name="Send")).to_be_enabled()


def echoed_attachments(page, replies):
    """The attachments the echo model said it was given in its latest
    reply, once the conversation has that many replies."""
    playwright.expect(page.locator(".agent-message-assistant")).to_have_count(replies)
    playwright.expect(page.get_by_role("button", name="Send")).to_be_enabled()
    return page.evaluate("""async () => {
          const url = document.querySelector(".agent-chat").dataset.conversationUrl;
          const markdown = await (await fetch(url + "/markdown")).text();
          const replies = markdown.split("## Agent").slice(1);
          return JSON.parse(replies[replies.length - 1].split("## User")[0]).attachments;
        }""")


def test_attach_from_index_page_and_send(page, server):
    assert page.goto(server["base"] + "/-/agent").status == 200
    attach = page.get_by_role("button", name="Attach files")
    playwright.expect(attach).to_be_visible()
    assert page.locator("#attach-input").get_attribute("accept") == (
        "image/gif,image/jpeg,image/png"
    )

    # The button opens the file chooser; the file uploads straight away
    with page.expect_file_chooser() as chooser:
        attach.click()
    chooser.value.set_files(PNG_FILE)
    playwright.expect(done_chips(page)).to_have_count(1)
    playwright.expect(done_chips(page)).to_contain_text("shot.png")

    page.fill("#new-message", "What is this?")
    page.get_by_role("button", name="Start chat").click()
    page.wait_for_url("**/-/agent/*")

    # The sent message shows the file as a thumbnail linking to its page
    thumbnail = page.locator(".agent-message-user .agent-attachment")
    playwright.expect(thumbnail).to_have_count(1)
    href = thumbnail.get_attribute("href")
    assert href.startswith(server["prefix"] + "/-/files/df-")
    image = thumbnail.locator("img")
    assert image.get_attribute("src") == href + "/thumbnail"
    assert image.get_attribute("alt") == "shot.png"
    page.wait_for_function(
        "() => document.querySelector('.agent-attachment img').naturalWidth > 0"
    )
    assert [a["type"] for a in echoed_attachments(page, 1)] == ["image/png"]

    # And still does when the page is rendered from what was saved
    page.reload()
    thumbnail = page.locator(".agent-message-user .agent-attachment")
    playwright.expect(thumbnail).to_have_count(1)
    assert thumbnail.get_attribute("href") == href
    page.wait_for_function(
        "() => document.querySelector('.agent-attachment img').naturalWidth > 0"
    )
    playwright.expect(page.locator(".agent-attachment-chip")).to_have_count(0)


def test_paste_drop_and_remove_in_conversation(page, server):
    start_chat(page, server)
    chips = page.locator("#attachment-chips")
    playwright.expect(chips).to_be_hidden()
    playwright.expect(page.get_by_role("button", name="Attach files")).to_be_visible()

    page.evaluate(
        DISPATCH_FILE_EVENT, ["#message-input", "paste", "pasted.png", PNG_BASE64]
    )
    playwright.expect(done_chips(page)).to_have_count(1)
    page.evaluate(
        DISPATCH_FILE_EVENT, ["#chat-form", "drop", "dropped.png", PNG_BASE64]
    )
    playwright.expect(done_chips(page)).to_have_count(2)
    page.evaluate(
        DISPATCH_FILE_EVENT, ["#message-input", "drop", "extra.png", PNG_BASE64]
    )
    playwright.expect(done_chips(page)).to_have_count(3)
    playwright.expect(chips).to_be_visible()
    assert not page.locator(".agent-attachment-dragover").count()

    page.get_by_role("button", name="Remove extra.png").click()
    playwright.expect(done_chips(page)).to_have_count(2)

    page.fill("#message-input", "Two files")
    page.get_by_role("button", name="Send").click()
    thumbnails = page.locator(".agent-message-user").last.locator(".agent-attachment")
    playwright.expect(thumbnails).to_have_count(2)
    assert [
        thumbnails.nth(i).locator("img").get_attribute("alt") for i in range(2)
    ] == ["pasted.png", "dropped.png"]
    # Sent, so the composer is empty again
    playwright.expect(chips).to_be_hidden()
    assert [a["type"] for a in echoed_attachments(page, 2)] == ["image/png"] * 2

    # A message with nothing attached sends none
    playwright.expect(page.get_by_role("button", name="Send")).to_be_enabled()
    page.fill("#message-input", "No files")
    page.get_by_role("button", name="Send").click()
    playwright.expect(page.locator(".agent-message-user")).to_have_count(3)
    assert echoed_attachments(page, 3) == []
    playwright.expect(
        page.locator(".agent-message-user").last.locator(".agent-attachment")
    ).to_have_count(0)


def test_unsupported_file_is_refused_in_the_composer(page, server):
    start_chat(page, server)
    page.set_input_files(
        "#attach-input",
        {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"hello"},
    )
    chip = page.locator(".agent-attachment-chip")
    playwright.expect(chip).to_have_count(1)
    playwright.expect(chip).to_have_attribute("data-status", "error")
    playwright.expect(chip).to_contain_text("does not accept text/plain")

    # It is not sent with the message
    page.fill("#message-input", "Anything attached?")
    page.get_by_role("button", name="Send").click()
    playwright.expect(page.locator(".agent-message-user")).to_have_count(2)
    assert echoed_attachments(page, 2) == []


def test_pasting_text_is_left_alone(page, server):
    start_chat(page, server)
    page.evaluate("""() => {
          const transfer = new DataTransfer();
          transfer.setData("text/plain", "just text");
          const event = new ClipboardEvent("paste", {
            clipboardData: transfer, bubbles: true, cancelable: true,
          });
          document.querySelector("#message-input").dispatchEvent(event);
          window.pastePrevented = event.defaultPrevented;
        }""")
    assert page.evaluate("window.pastePrevented") is False
    playwright.expect(page.locator(".agent-attachment-chip")).to_have_count(0)
