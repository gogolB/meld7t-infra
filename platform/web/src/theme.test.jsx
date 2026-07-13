import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import {
  resolveTheme, storedTheme, ThemeControl, ThemeProvider, THEME_STORAGE_KEY,
} from "./theme.jsx";

describe("color theme", () => {
  beforeEach(() => {
    window.localStorage.clear();
    delete document.documentElement.dataset.theme;
    delete document.documentElement.dataset.themePreference;
  });

  it("resolves system, light, and dark preferences", () => {
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
  });

  it("ignores an invalid stored preference", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "sepia");
    expect(storedTheme()).toBe("system");
  });

  it("persists an accessible light or dark selection", async () => {
    const user = userEvent.setup();
    render(<ThemeProvider><ThemeControl /></ThemeProvider>);
    await user.selectOptions(screen.getByRole("combobox", { name: "Color theme" }), "dark");

    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("dark"));
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(document.documentElement.dataset.themePreference).toBe("dark");
  });
});
