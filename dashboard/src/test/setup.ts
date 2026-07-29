import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  if (typeof localStorage.clear === "function") localStorage.clear();
  window.location.hash = "";
});
