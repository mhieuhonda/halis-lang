#!/usr/bin/env node
// tools/jsffi_check.js — Stage 73 (v0.92.0-alpha) acceptance checker.
//
// Usage: node tools/jsffi_check.js <wasm-path> <glue-js-path>
//
// Verifies, against a freshly compiled jsffi Stage 73 demo:
//   1. the wasm + JS glue instantiate cleanly (48 std.jsffi defaults)
//   2. hl_struct_descriptors() is exported and the glue AUTO-registers
//      every declared struct (no manual Halis.registerStruct calls)
//   3. a struct round-trips through Halis.writeStruct/readStruct
//      ("a Halis struct becomes a JS object" — the roadmap's promise)
//   4. JS -> HLS callbacks work: Halis.callHalis lands in the
//      program's jsffi_on_callback and returns processed JSON
//   5. main() runs in wasm and prints its markers (exit code 0)
//
// Exits non-zero with a FAIL message on the first broken invariant.

"use strict";

const fs = require("fs");

function fail(msg) {
  console.error("FAIL: " + msg);
  process.exit(1);
}

async function main() {
  const wasmPath = process.argv[2];
  const gluePath = process.argv[3];
  if (!wasmPath || !gluePath) {
    fail("usage: node tools/jsffi_check.js <wasm> <glue.js>");
  }
  const wasmBytes = fs.readFileSync(wasmPath);
  const glue = fs.readFileSync(gluePath, "utf-8");
  // Load the glue (defines the global Halis object).
  eval(glue);

  // 1) Instantiate (the glue auto-registers struct descriptors from
  //    hl_struct_descriptors() right after instantiation).
  const { instance } = await Halis.instantiate(
    new Uint8Array(wasmBytes));
  console.log("  instantiate:            ok");

  // 2) AUTO-registered struct descriptors.
  if (!Halis.structs || !Halis.structs.Point
      || !Halis.structs.Measurement) {
    fail("struct descriptors not auto-registered");
  }
  console.log("  auto-registered structs: "
    + Object.keys(Halis.structs).sort().join(","));

  // 3) Struct round trip WITHOUT manual registration.
  const ptr = Halis.writeStruct(instance.exports.hl_alloc, {
    x: 1.5, y: -2.25, tag: "demo", active: true, count: 7,
  }, "Point");
  const obj = Halis.readStruct(ptr, "Point");
  if (obj.x !== 1.5 || obj.y !== -2.25 || obj.tag !== "demo"
      || obj.active !== true || obj.count !== 7n) {
    fail("struct round trip mismatch: "
      + JSON.stringify(obj, (k, v) => typeof v === "bigint"
        ? Number(v) : v));
  }
  console.log("  struct round trip:      ok (Point -> JS object)");

  // 4) JS -> HLS callbacks.
  const echo = await Halis.callHalis(
    1, '{"cmd":"echo","msg":"from JS"}');
  const parsed = JSON.parse(echo);
  if (parsed.received !== true || parsed.echo.msg !== "from JS") {
    fail("echo callback mismatch: " + echo);
  }
  console.log("  JS->HLS callback echo:  ok");
  const pong = await Halis.callHalis(2, "ping please");
  if (pong !== "pong") {
    fail("ping callback mismatch: " + pong);
  }
  console.log("  JS->HLS callback ping:  ok");

  // 5) main() runs in wasm and prints its markers (capture stdout
  //    through console.log; the demo's final marker must appear).
  const captured = [];
  const origLog = console.log;
  console.log = function () {
    captured.push(Array.prototype.join.call(arguments, " "));
    origLog.apply(console, arguments);
  };
  let code = 0;
  try {
    code = await Halis.run(new Uint8Array(wasmBytes));
  } finally {
    console.log = origLog;
  }
  if (Number(code) !== 0) {
    fail("main exit code " + code);
  }
  if (!captured.join("\n").includes("=== jsffi demo done ===")) {
    fail("main markers missing");
  }
  console.log("  main() in wasm:         ok (exit 0, markers present)");
}

main().catch((e) => fail(e.message));
