const test = require("node:test");
const assert = require("node:assert/strict");

const {
  PROJECT_ROUTE_RE,
  parseArgs,
  projectLocatorEntries,
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

test("project lookup prioritizes the directory grid cell and never clicks bare text", () => {
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

  assert.equal(entries[0][0], "project grid cell");
  assert.equal(calls[0].method, "getByText");
  assert.equal(calls[1].selector, '[role="gridcell"]');
  assert.equal(entries[0][1].filterOptions.has.description, "text:DND AI AUTO");
  assert.equal(calls[2].selector, '[role="row"]');
  assert.ok(calls.some((call) => call.method === "getByText"));
  assert.match(entries.at(-1)[1].ancestorSelector, /ancestor::/);
});
