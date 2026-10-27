import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
  document.documentElement.className = "";
  document.documentElement.removeAttribute("style");
  vi.unstubAllGlobals();
});
