// Runs the pure top-level helper functions in glosa/web/static/js/station.js
// under Node -- no WebSocket/AudioContext/getUserMedia stubbing (station.js
// has no other JS test coverage; see tests/web/test_station_js.py for why
// these two decisions are pulled out as small pure functions instead of a
// full harness like room.js's). `document.getElementById` is stubbed just
// enough for the file's own IIFE to bail out immediately (`if (!configEl)
// return;`), leaving the pure functions it declares above that IIFE intact
// on the context. Reads {"fn": name, "args": [...]} JSON from stdin, calls
// that function, and prints {"result": ...} as JSON.
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { URL, URLSearchParams } = require("url");

const STATION_JS = path.join(__dirname, "..", "..", "glosa", "web", "static", "js", "station.js");

const context = { document: { getElementById: () => null }, URL, URLSearchParams };
vm.createContext(context);
vm.runInContext(fs.readFileSync(STATION_JS, "utf8"), context, { filename: "station.js" });

const { fn, args } = JSON.parse(fs.readFileSync(0, "utf8"));
if (typeof context[fn] !== "function") {
  throw new Error(`station.js has no pure top-level function named ${fn}`);
}
const result = context[fn](...args);
process.stdout.write(JSON.stringify({ result }));
