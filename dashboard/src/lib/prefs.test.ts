import { afterEach, describe, expect, it } from "vitest";
import { readRecord } from "./prefs";

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
