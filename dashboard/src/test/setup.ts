import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(String(key)) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => values.delete(String(key)),
    setItem: (key, value) => values.set(String(key), String(value)),
  };
}

// Node 25 exposes an empty localStorage object unless a backing file is set,
// which prevents Vitest's jsdom environment from installing Web Storage.
if (typeof globalThis.localStorage?.setItem !== "function") {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: memoryStorage(),
  });
}

afterEach(() => {
  cleanup();
  if (typeof localStorage.clear === "function") localStorage.clear();
  window.location.hash = "";
});
