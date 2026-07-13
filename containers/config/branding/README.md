# Bundled deployment branding

`report-logo.png` is the Houston Methodist **Leading Medicine** four-color PNG selected for this
deployment. The same bytes are served to the browser and embedded in newly generated combined
reports.

- Official source: [Houston Methodist Leading Medicine 4C PNG](https://www.houstonmethodist.org/-/media/files/marketing/brand/logos/hospital_and_system_logos/leading_medicine/4c/methodist_leading_medicine_4c_png.ashx?mw=1382&hash=6867B22F0D3D16FED9AB9575C268BC02)
- Retrieved: 2026-07-12
- Format and dimensions: PNG, indexed color, 1010 × 298 pixels
- Size: 10,802 bytes
- SHA-256: `f49320e2faddc9e4c8a650d9e90a03abb7325d1990fa2666849e911301c67f96`

The production installer copies this signed asset to the release-specific runtime branding
directory. A deployment may replace it by staging a different regular file at
`~/meld7t-secrets/production/branding/report-logo.png`; the existing ownership, decoding, size,
dimension, and immutable report-snapshot checks apply to either source.
