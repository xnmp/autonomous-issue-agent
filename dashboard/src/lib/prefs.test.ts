import { afterEach, describe, expect, it } from "vitest";
import { readRecord, readTimestamp } from "./prefs";

afterEach(() => localStorage.clear());

describe("readRecord", () => {
  it.each([
    ["absent", null],
    ["empty", ""],
    ["truncated", '{"a":{"b":'],
    ["a non-object", "7"],
    ["an array", "[1]"],
  ])("treats %s storage as an empty record", (_name, stored) => {
    if (stored !== null) localStorage.setItem("k", stored);
    expect(readRecord("k")).toEqual({});
  });

  it("returns a stored object", () => {
    localStorage.setItem("k", '{"a":1}');
    expect(readRecord("k")).toEqual({ a: 1 });
  });
});

describe("readTimestamp", () => {
  it.each([["absent", null], ["blank", " "], ["text", "abc"], ["zero", "0"], ["negative", "-1"], ["infinite", "Infinity"]])(
    "returns null for %s", (_n, stored) => {
      if (stored !== null) localStorage.setItem("t", stored);
      expect(readTimestamp("t")).toBeNull();
    });
  it("returns a stored positive number", () => {
    localStorage.setItem("t", "1700000000000");
    expect(readTimestamp("t")).toBe(1700000000000);
  });
});
