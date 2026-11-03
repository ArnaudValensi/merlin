// The Jobs page shows when a job runs next: timeAgo only knows the past.
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "..", "static", "dashboard.js"),
  "utf8",
);
const body = source.match(/function timeUntil\(isoString\) \{[\s\S]*?\n\}/)[0];
const context = {};
vm.runInNewContext(`${body}; this.timeUntil = timeUntil;`, context);

const inSeconds = (s) => new Date(Date.now() + s * 1000).toISOString();

test("says when, in the future", () => {
  assert.equal(context.timeUntil(inSeconds(5)), "now");
  assert.equal(context.timeUntil(inSeconds(330)), "in 5m");
  assert.equal(context.timeUntil(inSeconds(7300)), "in 2h");
  assert.equal(context.timeUntil(inSeconds(60 * 86400 + 100)), "in 60d");
});

test("a time already past is now", () => {
  assert.equal(context.timeUntil(inSeconds(-500)), "now");
});
