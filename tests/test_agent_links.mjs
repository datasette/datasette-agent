import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../datasette_agent/static/agent-links.js", import.meta.url), "utf8");
const {setLinkDestination} = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const base = "https://datasette.example/prefix/-/agent/conversation";

function withLink(callback) {
    const previousDocument = globalThis.document;
    const previousWindow = globalThis.window;
    const attributes = new Map();
    const classes = new Set();
    const assignedHrefs = [];
    const link = {
        hasAttribute: name => attributes.has(name),
        getAttribute: name => attributes.get(name),
        removeAttribute: name => attributes.delete(name),
        setAttribute(name, value) {
            if (name === "href") {
                // Even a transient external href could bypass confirmation.
                assert.equal(new URL(value, base).origin, new URL(base).origin);
                assignedHrefs.push(value);
            }
            attributes.set(name, value);
        },
        classList: {
            add: name => classes.add(name),
            remove: name => classes.delete(name),
        },
    };
    globalThis.document = {baseURI: base};
    globalThis.window = {location: new URL(base)};
    try {
        callback(link, assignedHrefs);
    } finally {
        if (previousDocument === undefined) delete globalThis.document;
        else globalThis.document = previousDocument;
        if (previousWindow === undefined) delete globalThis.window;
        else globalThis.window = previousWindow;
    }
}

for (const value of [
    "/prefix/demo/items?secret=123#row",
    "../another-conversation",
    "?query=123",
    "#section",
    "https://datasette.example/prefix/demo",
    "https://datasette.example:443/prefix/demo",
    "//datasette.example/prefix/demo",
]) {
    test("internal links retain native navigation: " + value, () => withLink(link => {
        setLinkDestination(link, value);
        assert.equal(link.getAttribute("href"), value);
        assert.equal(link.hasAttribute("data-agent-external-url"), false);
        assert.equal(link.hasAttribute("role"), false);
    }));
}

for (const value of [
    "https://external.example/leak?secret=123#section",
    "//external.example/leak?secret=123",
    "http://datasette.example/leak",
    "https://datasette.example:8443/leak",
    "https://datasette.example.external.example/leak",
    "https://datasette.example@external.example/leak",
    "https://external.example/\"<script>alert(1)</script>",
    "\\\\external.example/leak",
]) {
    test("external links require confirmation: " + value, () => withLink((link, assignedHrefs) => {
        setLinkDestination(link, value);
        assert.equal(link.hasAttribute("href"), false);
        assert.deepEqual(assignedHrefs, []);
        assert.equal(link.getAttribute("data-agent-external-url"), new URL(value, base).href);
        assert.equal(link.getAttribute("role"), "link");
        assert.equal(link.getAttribute("tabindex"), "0");
    }));
}

for (const value of [
    "javascript:alert(1)",
    "java\nscript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "blob:https://external.example/document",
    "file:///etc/passwd",
    "mailto:external@example.com",
    "https://[invalid",
]) {
    test("unsupported destinations stay inert: " + value, () => withLink(link => {
        setLinkDestination(link, value);
        assert.equal(link.hasAttribute("href"), false);
        assert.equal(link.hasAttribute("data-agent-external-url"), false);
    }));
}

test("changing a link destination clears the previous navigation behavior", () => withLink(link => {
    setLinkDestination(link, "/prefix/demo");
    setLinkDestination(link, "https://external.example/leak");
    assert.equal(link.hasAttribute("href"), false);
    setLinkDestination(link, "/prefix/another");
    assert.equal(link.getAttribute("href"), "/prefix/another");
    assert.equal(link.hasAttribute("data-agent-external-url"), false);
    assert.equal(link.hasAttribute("role"), false);
    assert.equal(link.hasAttribute("tabindex"), false);
    setLinkDestination(link, "javascript:alert(1)");
    assert.equal(link.hasAttribute("href"), false);
}));
