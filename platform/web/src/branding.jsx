import React, { createContext, useContext, useEffect, useMemo, useState } from "react";
import { api } from "./api.js";

export const DEFAULT_BRANDING = Object.freeze({
  productName: "MELD 7T",
  institutionName: "Houston Methodist",
  departmentName: "Houston Methodist Research Institute",
  logoUrl: "/branding/report-logo.png",
  primaryColor: "#124A7E",
  secondaryColor: "#749ABB",
  footerText: "Houston Methodist Research Institute",
});

const BrandingContext = createContext(DEFAULT_BRANDING);
const HEX_COLOR = /^#[0-9a-f]{6}$/i;

function textValue(payload, camelName, snakeName, fallback, maxLength) {
  const raw = payload?.[camelName] ?? payload?.[snakeName];
  if (typeof raw !== "string") return fallback;
  const value = raw.replace(/\s+/g, " ").trim();
  return value && value.length <= maxLength ? value : fallback;
}

function safeLogoUrl(payload) {
  const raw = payload?.logoUrl ?? payload?.logo_url;
  if (typeof raw !== "string") return "";
  const value = raw.trim();
  if (!/^\/branding\/[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(value)) {
    return "";
  }
  const parts = value.slice("/branding/".length).split("/");
  return parts.length > 0 && parts.every((part) => part && part !== "." && part !== "..")
    ? value : "";
}

function colorValue(payload, camelName, snakeName, fallback) {
  const raw = payload?.[camelName] ?? payload?.[snakeName];
  return typeof raw === "string" && HEX_COLOR.test(raw) ? raw.toUpperCase() : fallback;
}

export function normalizeBranding(payload) {
  return Object.freeze({
    productName: textValue(payload, "productName", "product_name",
      DEFAULT_BRANDING.productName, 80),
    institutionName: textValue(payload, "institutionName", "institution_name",
      DEFAULT_BRANDING.institutionName, 120),
    departmentName: textValue(payload, "departmentName", "department_name",
      DEFAULT_BRANDING.departmentName, 160),
    logoUrl: safeLogoUrl(payload),
    primaryColor: colorValue(payload, "primaryColor", "primary_color",
      DEFAULT_BRANDING.primaryColor),
    secondaryColor: colorValue(payload, "secondaryColor", "secondary_color",
      DEFAULT_BRANDING.secondaryColor),
    footerText: textValue(payload, "footerText", "footer_text",
      DEFAULT_BRANDING.footerText, 240),
  });
}

function hexRgb(value) {
  return [1, 3, 5].map((offset) => Number.parseInt(value.slice(offset, offset + 2), 16));
}

function contrastInk(color) {
  const channels = hexRgb(color).map((value) => {
    const normalized = value / 255;
    return normalized <= 0.04045
      ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
  });
  const luminance = 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  return luminance > 0.45 ? "#101820" : "#FFFFFF";
}

export function applyBranding(branding, documentRef = document) {
  const root = documentRef.documentElement;
  root.style.setProperty("--brand", branding.primaryColor);
  root.style.setProperty("--brand-secondary", branding.secondaryColor);
  root.style.setProperty("--brand-contrast", contrastInk(branding.primaryColor));
  documentRef.title = `${branding.productName} · ${branding.departmentName}`;
  documentRef.querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", branding.primaryColor);
}

export function BrandingProvider({ children, load = api.branding }) {
  const [branding, setBranding] = useState(DEFAULT_BRANDING);

  useEffect(() => {
    let active = true;
    Promise.resolve().then(load).then((payload) => {
      if (active) setBranding(normalizeBranding(payload));
    }).catch(() => {
      if (active) setBranding(DEFAULT_BRANDING);
    });
    return () => { active = false; };
  }, [load]);

  useEffect(() => { applyBranding(branding); }, [branding]);
  const value = useMemo(() => branding, [branding]);
  return <BrandingContext.Provider value={value}>{children}</BrandingContext.Provider>;
}

export function useBranding() {
  return useContext(BrandingContext);
}

export function BrandIdentity() {
  const branding = useBranding();
  const [logoFailed, setLogoFailed] = useState(false);
  useEffect(() => { setLogoFailed(false); }, [branding.logoUrl]);

  return (
    <div className="brand-identity">
      {branding.logoUrl && !logoFailed && (
        <img className="brand-logo" src={branding.logoUrl} alt="" aria-hidden="true"
          onError={() => setLogoFailed(true)} />
      )}
      <div className="brand-copy">
        <span className="brand-product">{branding.productName}</span>
        <span className="brand-department">{branding.departmentName}</span>
      </div>
    </div>
  );
}
