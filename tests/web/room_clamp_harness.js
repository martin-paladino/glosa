// Calls glosaClampDragPosition(...) from room.js directly: it's declared at
// the top of that file, outside the closure, exactly so it can be tested
// with no fake DOM at all (see room.js's comment above it). Reads
// {"x","y","panelW","panelH","viewportW","viewportH","margin"} from stdin,
// prints the clamped {"x","y"}. Used by tests/web/test_room_js.py.
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOM_JS = path.join(__dirname, "..", "..", "glosa", "web", "static", "js", "room.js");

// Just enough of `document` for the closure below the pure function to bail
// out at its first line (`if (!configEl) return;`) without touching anything.
const context = { document: { getElementById: () => null } };
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(ROOM_JS, "utf8"), context, { filename: "room.js" });

const { x, y, panelW, panelH, viewportW, viewportH, margin } = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(context.glosaClampDragPosition(x, y, panelW, panelH, viewportW, viewportH, margin)));
