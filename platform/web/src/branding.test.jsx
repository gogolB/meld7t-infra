import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  applyBranding, BrandIdentity, BrandingProvider, DEFAULT_BRANDING, normalizeBranding,
} from "./branding.jsx";

describe("deployment branding", () => {
  it("uses the bundled Houston Methodist mark before runtime settings load", () => {
    expect(DEFAULT_BRANDING.logoUrl).toBe("/branding/report-logo.png");
  });

  it("normalizes the authenticated API payload", () => {
    expect(normalizeBranding({
      product_name: "Research Imaging",
      institution_name: "Example Hospital",
      department_name: "Imaging Institute",
      logo_url: "/branding/site-mark.svg",
      primary_color: "#123456",
      secondary_color: "#abcdef",
      footer_text: "Imaging research",
    })).toEqual({
      productName: "Research Imaging",
      institutionName: "Example Hospital",
      departmentName: "Imaging Institute",
      logoUrl: "/branding/site-mark.svg",
      primaryColor: "#123456",
      secondaryColor: "#ABCDEF",
      footerText: "Imaging research",
    });
  });

  it("falls back safely for remote logos and malformed colors", () => {
    const result = normalizeBranding({
      logo_url: "https://tracking.example/logo.svg",
      primary_color: "red",
      secondary_color: "#12345g",
    });
    expect(result.logoUrl).toBe("");
    expect(result.primaryColor).toBe(DEFAULT_BRANDING.primaryColor);
    expect(result.secondaryColor).toBe(DEFAULT_BRANDING.secondaryColor);
  });

  it("applies the white label to document metadata and design tokens", () => {
    const branding = normalizeBranding({
      product_name: "Research Suite", department_name: "Neuro Lab",
      primary_color: "#FFFFFF", secondary_color: "#111111",
    });
    applyBranding(branding);
    expect(document.title).toBe("Research Suite · Neuro Lab");
    expect(document.documentElement.style.getPropertyValue("--brand")).toBe("#FFFFFF");
    expect(document.documentElement.style.getPropertyValue("--brand-contrast")).toBe("#101820");
  });

  it("renders an operator-supplied identity after runtime loading", async () => {
    render(<BrandingProvider load={() => Promise.resolve({
      product_name: "Site Platform", department_name: "Site Research",
      institution_name: "Site Hospital", footer_text: "Site footer",
    })}><BrandIdentity /></BrandingProvider>);

    await waitFor(() => expect(screen.getByText("Site Platform")).toBeInTheDocument());
    expect(screen.getByText("Site Research")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
