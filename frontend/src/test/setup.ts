import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { resetDemoUser } from "./utils";

// jsdom does not implement scrolling.
Element.prototype.scrollIntoView = vi.fn();

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  resetDemoUser();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
