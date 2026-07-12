import React, { createContext, useContext, useEffect, useMemo, useState } from "react";

export const THEME_STORAGE_KEY = "meld7t.theme";
export const THEME_PREFERENCES = Object.freeze(["system", "light", "dark"]);

const ThemeContext = createContext({
  preference: "system", resolvedTheme: "light", setPreference: () => {},
});

export function storedTheme(storage = window.localStorage) {
  try {
    const value = storage.getItem(THEME_STORAGE_KEY);
    return THEME_PREFERENCES.includes(value) ? value : "system";
  } catch {
    return "system";
  }
}

export function resolveTheme(preference, systemDark) {
  return preference === "system" ? (systemDark ? "dark" : "light") : preference;
}

function darkMedia() {
  return typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: dark)") : null;
}

export function ThemeProvider({ children }) {
  const [preference, setPreferenceState] = useState(() => storedTheme());
  const [systemDark, setSystemDark] = useState(() => Boolean(darkMedia()?.matches));
  const resolvedTheme = resolveTheme(preference, systemDark);

  useEffect(() => {
    const media = darkMedia();
    if (!media) return undefined;
    const update = (event) => setSystemDark(event.matches);
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = resolvedTheme;
    document.documentElement.dataset.themePreference = preference;
    document.documentElement.style.colorScheme = resolvedTheme;
  }, [preference, resolvedTheme]);

  function setPreference(value) {
    const next = THEME_PREFERENCES.includes(value) ? value : "system";
    setPreferenceState(next);
    try { window.localStorage.setItem(THEME_STORAGE_KEY, next); } catch { /* unavailable */ }
  }

  const value = useMemo(() => ({ preference, resolvedTheme, setPreference }),
    [preference, resolvedTheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  return useContext(ThemeContext);
}

export function ThemeControl() {
  const { preference, resolvedTheme, setPreference } = useTheme();
  return (
    <label className="theme-control" title={`Current appearance: ${resolvedTheme}`}>
      <span aria-hidden="true" className="theme-icon">{resolvedTheme === "dark" ? "☾" : "☀"}</span>
      <span className="sr-only">Color theme</span>
      <select aria-label="Color theme" value={preference}
        onChange={(event) => setPreference(event.target.value)}>
        <option value="system">System</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>
    </label>
  );
}
