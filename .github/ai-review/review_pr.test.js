const test = require("node:test");
const assert = require("node:assert/strict");

const {
  PROJECT_ROUTE_RE,
  parseArgs,
  projectLocatorEntries,
  projectNewChatButton,
} = require("./review_pr.js");

test("project routes distinguish a project chat from the Projects directory", () => {
  assert.match("https://chatgpt.com/g/g-project/project", PROJECT_ROUTE_RE);
  assert.match("https://chatgpt.com/g/g-project/c/conversation", PROJECT_ROUTE_RE);
  assert.doesNotMatch("https://chatgpt.com/projects", PROJECT_ROUTE_RE);
});

test("preflight is parsed without requiring a PR URL", () => {
  assert.deepEqual(parseArgs(["--preflight"]), { help: false, preflight: true });
  assert.deepEqual(parseArgs(["--help"]), { help: true, preflight: false });
});

test("project lookup opens through the row's new-chat button, never the bare text", () => {
  const calls = [];
  const fakeLocator = (description) => ({
    description,
    first() {
      return this;
    },
    filter(options) {
      this.filterOptions = options;
      return this;
    },
    locator(selector) {
      this.ancestorSelector = selector;
      return this;
    },
    getByRole(role, options) {
      calls.push({ method: "chainedGetByRole", role, options, on: description });
      return fakeLocator(`${description} >> ${role}:${options.name}`);
    },
  });
  const page = {
    getByRole(role, options) {
      calls.push({ method: "getByRole", role, options });
      return fakeLocator(`${role}:${options.name}`);
    },
    getByText(text, options) {
      calls.push({ method: "getByText", text, options });
      return fakeLocator(`text:${text}`);
    },
    locator(selector) {
      calls.push({ method: "locator", selector });
      return fakeLocator(selector);
    },
  };

  const entries = projectLocatorEntries(page, "DND AI AUTO");

  // Redesigned Projects directory: rows are plain text, so the first entry
  // must be the row-scoped "Start new chat in project" button.
  assert.equal(entries[0][0], "project new chat button");
  assert.equal(entries[1][0], "project row wrapper");
  assert.equal(entries[2][0], "project grid cell");
  assert.ok(
    calls.some(
      (call) =>
        call.method === "chainedGetByRole" &&
        call.role === "button" &&
        /start new chat in project/i.test(String(call.options.name)),
    ),
  );
  assert.ok(
    calls.some(
      (call) => call.method === "locator" && call.selector === "div[data-project-row-wrapper]",
    ),
  );
  assert.ok(calls.some((call) => call.method === "getByText"));
  assert.match(entries.at(-1)[1].ancestorSelector, /ancestor::/);
});

test("project new-chat button is scoped to the matching project row", () => {
  const calls = [];
  const fakes = [];
  const fakeLocator = (description) => {
    const fake = {
      description,
      first() {
        return this;
      },
      filter(options) {
        this.filterOptions = options;
        return this;
      },
      getByRole(role, options) {
        calls.push({ role, options, on: description });
        return fakeLocator(`${description} >> button`);
      },
    };
    fakes.push(fake);
    return fake;
  };
  const page = {
    getByText(text, options) {
      return fakeLocator(`text:${text}`);
    },
    locator(selector) {
      calls.push({ selector });
      return fakeLocator(selector);
    },
  };

  projectNewChatButton(page, "DND AI AUTO");

  const wrapper = fakes.find((fake) => fake.description === "div[data-project-row-wrapper]");
  assert.equal(wrapper.filterOptions.has.description, "text:DND AI AUTO");
  assert.equal(calls.at(-1).role, "button");
  assert.match(String(calls.at(-1).options.name), /start new chat in project/i);
});
